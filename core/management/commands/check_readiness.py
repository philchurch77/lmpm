from __future__ import annotations

from collections import Counter

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from appraisals.models import AcademicYear
from core.models import SchoolProfile, StaffMember


class Command(BaseCommand):
    help = (
        "Audit the live data for onboarding dead-ends that block a smooth first "
        "login. Read-only: writes nothing. Reports who is unclassified, who has "
        "no login, who can log in but has no staff record, dangling manager "
        "links, and whether an appraisal cycle is open. Exits non-zero if any "
        "blocker is found (so it can gate a deploy/checklist)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Max emails to list per section (default 25).",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        blockers = 0
        warnings = 0

        # Build the identity picture once. Identity is by email (see core.identity):
        # there is no FK between User and StaffMember, so everything is a compare.
        staff = list(StaffMember.objects.select_related("school").order_by("email"))
        staff_emails = {s.email.strip().lower() for s in staff if s.email}
        active_user_emails = {
            (u.email or "").strip().lower()
            for u in User.objects.filter(is_active=True)
            if u.email
        }
        superuser_emails = {
            (u.email or "").strip().lower()
            for u in User.objects.filter(is_active=True, is_superuser=True)
            if u.email
        }
        profile_user_ids = set(
            SchoolProfile.objects.values_list("user_id", flat=True)
        )

        # 1. Is there an open appraisal cycle? (Global blocker — every staff
        #    member sees "no active appraisal cycle yet" without one.)
        if not AcademicYear.objects.filter(is_current=True).exists():
            blockers += 1
            self._section(
                "BLOCKER: No active appraisal cycle",
                ["No AcademicYear is marked is_current — nobody can start an "
                 "appraisal. Set one in the admin."],
            )
        else:
            current = AcademicYear.objects.filter(is_current=True).count()
            if current > 1:  # save() prevents this, but a bulk update could not.
                warnings += 1
                self._section(
                    "WARNING: Multiple current academic years",
                    [f"{current} AcademicYear rows have is_current=True; exactly "
                     "one is expected."],
                )

        # 2. Unclassified staff — the main first-time dead-end.
        unclassified = [s.email for s in staff if not s.staff_type]
        if unclassified:
            blockers += 1
            self._section(
                f"BLOCKER: {len(unclassified)} staff have no staff_type",
                unclassified,
                limit,
                note="They cannot start an appraisal until classified "
                "(Teaching/Support/Senior leader). Set via import, admin, or the "
                "in-app Teaching/Support choice.",
            )

        # 3. Staff with no login — a User must exist (by email) for SSO to work.
        #    Split deliberately: a staff member whose only matching User is
        #    DEACTIVATED has had their access revoked on purpose (that is how a
        #    leaver is offboarded). Reporting them as a blocker with "run
        #    provision_users" invited an administrator to hand a leaver their
        #    access back, and left a readiness check that could never go green.
        inactive_user_emails = {
            (u.email or "").strip().lower()
            for u in User.objects.filter(is_active=False)
            if u.email
        }
        never_had_login = sorted(
            staff_emails - active_user_emails - inactive_user_emails
        )
        revoked = sorted((staff_emails - active_user_emails) & inactive_user_emails)
        if never_had_login:
            blockers += 1
            self._section(
                f"BLOCKER: {len(never_had_login)} staff have no login account",
                never_had_login,
                limit,
                note="No Django User matches their email — save them in the "
                "admin (with a school set) or run `provision_users`.",
            )
        if revoked:
            self._section(
                f"INFO: {len(revoked)} staff have a deactivated login",
                revoked,
                limit,
                note="Treated as access deliberately revoked (a leaver), not a "
                "problem to fix. Provisioning refuses to reactivate them. If "
                "someone here has returned, reactivate their User.",
            )

        # 4. Staff who have a User but no SchoolProfile (fails the SSO gate).
        #    Superusers are exempt from the SchoolProfile requirement.
        users_by_email = {
            (u.email or "").strip().lower(): u
            for u in User.objects.filter(is_active=True)
            if u.email
        }
        no_profile = sorted(
            email
            for email in staff_emails & active_user_emails
            if email not in superuser_emails
            and users_by_email[email].id not in profile_user_ids
        )
        if no_profile:
            blockers += 1
            self._section(
                f"BLOCKER: {len(no_profile)} staff have a login but no SchoolProfile",
                no_profile,
                limit,
                note="They will be denied at login ('not configured with a "
                "school') — set a school on the StaffMember and save it in the "
                "admin, or run `provision_users`.",
            )

        # Everyone who can actually sign in today: an active, non-superuser login
        # that carries a SchoolProfile, plus the superusers who bypass that gate.
        provisioned_emails = (
            staff_emails & active_user_emails
        ) - set(no_profile) - set(never_had_login)

        # 5. Login but no StaffMember — they pass the SSO gate then hit "couldn't
        #    find a staff record" on every feature page. Exclude superusers (who
        #    legitimately act without a StaffMember).
        orphan_users = sorted(
            active_user_emails - staff_emails - superuser_emails
        )
        if orphan_users:
            warnings += 1
            self._section(
                f"WARNING: {len(orphan_users)} logins have no staff record",
                orphan_users,
                limit,
                note="They can sign in but every feature page shows 'couldn't "
                "find a staff record'. Create a StaffMember or deactivate the User.",
            )

        # 6. Staff with no school FK — provisioning skips these. Only a problem
        #    for people who cannot already sign in: clearing the school from
        #    someone already provisioned leaves their access intact, so warning
        #    that "they get no login" would be simply false for them.
        no_school = [
            s.email
            for s in staff
            if s.school is None
            and (s.email or "").strip().lower() not in provisioned_emails
        ]
        if no_school:
            warnings += 1
            self._section(
                f"WARNING: {len(no_school)} staff have no school",
                no_school,
                limit,
                note="Provisioning skips them (a SchoolProfile needs a school), "
                "so they get no login until one is set. Setting the school and "
                "saving in the admin finishes the job.",
            )

        # 6b. Two active logins sharing one email. auth.User.email is not
        #     unique, and the SSO gate can only sign the person into one of
        #     them — so if the other holds the SchoolProfile, access is a
        #     coin flip. core.provisioning refuses to provision these rather
        #     than guessing which account is the real one.
        email_counts = Counter(
            (u.email or "").strip().lower()
            for u in User.objects.filter(is_active=True)
            if (u.email or "").strip()
        )
        duplicate_logins = sorted(
            f"{email} ({count} active accounts)"
            for email, count in email_counts.items()
            if count > 1
        )
        if duplicate_logins:
            blockers += 1
            self._section(
                f"BLOCKER: {len(duplicate_logins)} email(s) have more than one login",
                duplicate_logins,
                limit,
                note="Which account they sign into is undefined, and their "
                "access cannot be set up until the duplicate is removed. "
                "Deactivate or delete the redundant User.",
            )

        # 6c. Emails that are not normalised. Migration 0005 lower-cased the
        #     existing data and both write paths keep it that way, so anything
        #     here arrived by a route that bypasses them (a bulk .update(), a
        #     raw SQL edit, a fixture). Reads are case-insensitive so nothing is
        #     broken today — it is reported because the *next* query that
        #     compares emails exactly would be wrong, silently.
        unnormalised = sorted(
            u.email
            for u in User.objects.exclude(email="")
            if u.email != u.email.strip().lower()
        ) + sorted(
            s.email
            for s in staff
            if s.email and s.email != s.email.strip().lower()
        )
        if unnormalised:
            warnings += 1
            self._section(
                f"WARNING: {len(unnormalised)} email(s) are not lower case",
                unnormalised,
                limit,
                note="Identity is matched on the email string. Re-saving the "
                "record in the admin normalises it.",
            )

        # 7. Dangling manager links — manager emails that resolve to nobody.
        dangling = []
        for s in staff:
            for field, value in (
                ("line_manager_email", s.line_manager_email),
                ("performance_manager_email", s.performance_manager_email),
            ):
                v = (value or "").strip().lower()
                if v and v not in staff_emails:
                    dangling.append(f"{s.email}: {field} -> {v} (no such staff)")
        if dangling:
            warnings += 1
            self._section(
                f"WARNING: {len(dangling)} dangling manager link(s)",
                dangling,
                limit,
                note="These line/performance-manager emails match no StaffMember, "
                "so the reporting relationship silently does nothing.",
            )

        # Summary + exit code.
        self.stdout.write("")
        if blockers == 0 and warnings == 0:
            self.stdout.write(self.style.SUCCESS("All readiness checks passed."))
            return
        style = self.style.ERROR if blockers else self.style.WARNING
        self.stdout.write(
            style(f"Readiness: {blockers} blocker(s), {warnings} warning(s).")
        )
        if blockers:
            # Non-zero exit so this can gate a checklist/CI step.
            raise SystemExit(1)

    def _section(self, title, rows, limit=None, note=None):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(title))
        if note:
            self.stdout.write(f"  {note}")
        shown = rows if limit is None else rows[:limit]
        for row in shown:
            self.stdout.write(f"  - {row}")
        if limit is not None and len(rows) > limit:
            self.stdout.write(f"  ... and {len(rows) - limit} more")

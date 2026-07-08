from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import SchoolProfile, StaffMember


class Command(BaseCommand):
    help = (
        "Give imported staff a login. For every StaffMember, ensure a matching "
        "Django User (by email) and a SchoolProfile (the SSO access gate) exist. "
        "Identity is by email, so the new User's email is set to the StaffMember's "
        "email; authentication is via Microsoft SSO, so the User gets an unusable "
        "local password. Idempotent — safe to re-run. Use --dry-run to preview."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        users_created = 0
        profiles_created = 0
        already_ok = 0
        skipped_no_school = 0
        skipped_superuser = 0

        # StaffMember.save() lowercases email, but staff may have been bulk-imported
        # bypassing save(); normalise here so the User email/username match is stable.
        for staff in StaffMember.objects.select_related("school").order_by("email"):
            email = (staff.email or "").strip().lower()
            if not email:
                continue

            user = User.objects.filter(email__iexact=email).first()

            # A superuser matching this email already has full access and needs no
            # SchoolProfile (see RestrictMicrosoftLoginAdapter) — never touch it.
            if user is not None and user.is_superuser:
                skipped_superuser += 1
                continue

            has_profile = user is not None and SchoolProfile.objects.filter(user=user).exists()
            if user is not None and has_profile:
                already_ok += 1
                continue

            # A SchoolProfile requires a school (the FK is mandatory). Without one on
            # the StaffMember we can't create a valid gate, so provisioning the User
            # alone would leave them unable to log in — skip and report instead.
            if staff.school is None:
                skipped_no_school += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"  SKIP {email}: StaffMember has no school; set one first."
                    )
                )
                continue

            will_create_user = user is None
            action = []
            if will_create_user:
                action.append("create User")
            if not has_profile:
                action.append("create SchoolProfile")
            self.stdout.write(f"  {email}: {', '.join(action)} (school: {staff.school})")

            if dry_run:
                if will_create_user:
                    users_created += 1
                if not has_profile:
                    profiles_created += 1
                continue

            with transaction.atomic():
                if user is None:
                    user = User.objects.create(
                        username=email[:150],
                        email=email,
                        is_active=True,
                    )
                    # SSO-only: no local password should ever authenticate this user.
                    user.set_unusable_password()
                    user.save(update_fields=["password"])
                    users_created += 1

                if not SchoolProfile.objects.filter(user=user).exists():
                    profile = SchoolProfile.objects.create(user=user, school=staff.school)
                    profile.schools.add(staff.school)
                    profiles_created += 1

        verb = "Would provision" if dry_run else "Provisioned"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {users_created} user(s), {profiles_created} SchoolProfile(s). "
                f"Already had login: {already_ok}. "
                f"Skipped (no school): {skipped_no_school}. "
                f"Skipped (superuser): {skipped_superuser}."
            )
        )

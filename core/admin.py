from collections import defaultdict

from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.db.models.functions import Lower

from .identity import normalise_email
from .models import Branding, School, SchoolProfile, StaffMember
from .provisioning import (
    LABEL_DEACTIVATED,
    LABEL_DUPLICATE,
    LABEL_NO_LOGIN,
    LABEL_NO_SCHOOL_LINK,
    LABEL_NO_SCHOOL_SET,
    LABEL_YES,
    OUTCOME_MESSAGES,
    ProvisionOutcome,
    annotate_login_state,
    describe,
    login_state_label,
    provision_staff_member,
    provision_staff_members,
)


admin.site.unregister(User)


@admin.register(User)
class NormalisingUserAdmin(DjangoUserAdmin):
    """Django's own user admin, plus email normalisation on save.

    ``auth.User`` is third-party and normalises its email nowhere, while every
    identity comparison in this project is an email string match against it (see
    :func:`core.identity.current_staff_member`). A hand-typed ``A.Green@…`` here
    therefore produced a user that reads matched the app's own records or not
    depending on whether the particular query remembered to be case-insensitive.
    The reads are all case-insensitive now, but normalising the write is what
    stops that being load-bearing in every future query.

    ``username`` is deliberately left alone — identity is by email, the username
    is cosmetic, and rewriting it risks colliding with its unique constraint.
    """

    def save_model(self, request, obj, form, change):
        obj.email = normalise_email(obj.email)
        super().save_model(request, obj, form, change)


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "phase")
    search_fields = ("name",)


@admin.register(SchoolProfile)
class SchoolProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "school")
    search_fields = ("user__username", "user__email", "school__name")
    autocomplete_fields = ("user", "school")


#: Above this many rows, the bulk action warns that the command is a better bet.
BULK_PROVISION_ADVISORY = 200

#: How many emails to name in a grouped "not set up" warning before summarising.
SKIP_SAMPLE_SIZE = 10


class LoginStateFilter(admin.SimpleListFilter):
    """Filter the staff list by whether the person can actually sign in.

    Reads the same annotations as the "Can sign in?" column (applied in
    ``StaffMemberAdmin.get_queryset``), so the two are driven by one source
    rather than two hand-written rules. Mainly for the post-import case: "show
    me everyone who still cannot sign in" is one click.

    ``yes`` and ``any_problem`` are written to partition the list between them:
    an administrator signs in fine without a SchoolProfile, so they belong in
    ``yes``, and a duplicate-account holder does not despite looking like one.
    """

    title = "can sign in"
    parameter_name = "login_state"

    def lookups(self, request, model_admin):
        # Labels come from provisioning.py so the filter options and the column
        # cannot drift apart — they agree by import, not by being typed twice.
        return (
            ("yes", LABEL_YES),
            ("no_school_set", LABEL_NO_SCHOOL_SET),
            ("no_login", LABEL_NO_LOGIN),
            ("no_school", LABEL_NO_SCHOOL_LINK),
            ("deactivated", LABEL_DEACTIVATED),
            ("duplicate", LABEL_DUPLICATE),
            ("any_problem", "Anyone who cannot sign in"),
        )

    def _can_sign_in(self):
        # Superusers bypass the SchoolProfile gate entirely; everyone else needs
        # exactly one active account and a profile on it.
        return Q(_is_admin=True) | Q(
            _has_login=True, _has_profile=True, _login_count=1
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value == "yes":
            return queryset.filter(self._can_sign_in())
        if value == "no_school_set":
            return queryset.filter(
                _has_login=False, _has_inactive_login=False, school__isnull=True
            )
        if value == "no_login":
            return queryset.filter(
                _has_login=False,
                _has_inactive_login=False,
                _is_admin=False,
                school__isnull=False,
            )
        if value == "no_school":
            return queryset.filter(
                _has_login=True, _has_profile=False, _is_admin=False, _login_count=1
            )
        if value == "deactivated":
            return queryset.filter(_has_login=False, _has_inactive_login=True)
        if value == "duplicate":
            return queryset.filter(_login_count__gt=1, _is_admin=False)
        if value == "any_problem":
            return queryset.exclude(self._can_sign_in())
        return queryset


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "job_title",
        "department",
        "staff_type",
        "school",
        "can_sign_in",
        "line_manager_email",
        "performance_manager_email",
    )
    # Edit staff_type inline from the list (per-row dropdown, one Save button).
    # email is the link column, so staff_type is a valid editable field.
    list_editable = ("staff_type",)
    list_filter = ("school", "department", "staff_type", LoginStateFilter)
    search_fields = (
        "email",
        "line_manager_email",
        "performance_manager_email",
        "job_title",
        "department",
    )
    autocomplete_fields = ("school",)
    actions = (
        "provision_logins",
        "set_type_teaching",
        "set_type_support",
        "set_type_leader",
    )

    def get_queryset(self, request):
        # One correlated subquery per flag, so the "Can sign in?" column costs
        # the same whether the trust has 30 staff or 3,000 — not an N+1.
        return annotate_login_state(super().get_queryset(request))

    # Deliberately not sortable: the label is driven by three flags, so ordering
    # by any one of them would sort "no login account" and "no school link"
    # identically and mix administrators in with the broken rows. A header that
    # sorts wrongly is worse than one that does not sort — LoginStateFilter is
    # the right tool for narrowing this down.
    @admin.display(description="Can sign in?")
    def can_sign_in(self, obj):
        return login_state_label(obj)

    def save_model(self, request, obj, form, change):
        """Save, then give this person a login if they can have one.

        Runs on *every* admin save, not just creation — which is the point.
        A staff member added before their school is known is reported as
        not-yet-provisionable; setting the school later and saving again
        finishes the job. The old management command instead skipped a
        school-less row and never revisited it, so assigning the school
        afterwards left the person permanently unable to sign in with nothing
        on screen to say why. Provisioning is idempotent, so re-saving an
        already-working staff member does nothing.

        Superuser-gated for the same reason ``provision_logins`` is, and it has
        to be gated *here* or that gate means nothing: ``list_editable`` makes
        Django's changelist POST call this method once per changed row, checking
        only ``has_change_permission``. Without this check a user with plain
        change rights could tick every row on the changelist, nudge a dropdown,
        and mint a live SSO account for each one — precisely the bulk capability
        the action's gate exists to prevent.
        """
        super().save_model(request, obj, form, change)

        if change and "email" in getattr(form, "changed_data", ()):
            # The old address keeps its own User and SchoolProfile, both still
            # active, so the previous login still passes the SSO gate — it just
            # lands on "couldn't find a staff record". Say so at the moment of
            # the change rather than leaving it for a later audit.
            self.message_user(
                request,
                "This staff member's email address changed. Their previous "
                "login still exists and still works — deactivate that user "
                "under Authentication and Authorization if it is no longer "
                "wanted.",
                level=messages.WARNING,
            )

        if not request.user.is_superuser:
            # Say so. A silent green tick with no login created is the exact
            # dead end this feature exists to remove — it would just move the
            # confusion onto a different person.
            if login_state_label(annotate_login_state(
                StaffMember.objects.filter(pk=obj.pk)
            ).first()) != LABEL_YES:
                self.message_user(
                    request,
                    "Saved. This person cannot sign in yet — only a trust "
                    "administrator can set up logins, so ask one to do it.",
                    level=messages.INFO,
                )
            return
        self._provision_one(request, obj)

    def get_actions(self, request):
        """Hide the provisioning action from anyone who cannot run it.

        Django lists actions regardless of the explicit ``is_superuser`` check
        inside ``provision_logins``, so leaving it visible offers a non-superuser
        a button whose only outcome is a bare 403 page.
        """
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("provision_logins", None)
        return actions

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self._warn_orphan_login(request, [obj])

    def delete_queryset(self, request, queryset):
        staff = list(queryset)
        super().delete_queryset(request, queryset)
        self._warn_orphan_login(request, staff)

    def _warn_orphan_login(self, request, staff_members):
        """Deleting a staff record does not remove their login.

        Deleting the StaffMember is the obvious way a non-technical admin tries
        to offboard someone, and it leaves the User and SchoolProfile untouched
        — so the person still passes the SSO gate and lands inside the app on
        "couldn't find a staff record". The email-change path already warns;
        this is the more likely route and warned nothing.
        """
        emails = [
            (s.email or "").strip().lower() for s in staff_members if (s.email or "").strip()
        ]
        # Case-insensitive, like every other email join in this feature.
        # auth.User.email is not normalised anywhere — provisioning lowercases
        # what it creates, but a hand-made or legacy account can be stored as
        # "Leaver@Oxlip.Test". Those are precisely the accounts provisioning did
        # NOT create, and an exact match would leave them unwarned: the admin
        # deletes the staff record, sees a clean success page, and the person
        # keeps working access.
        live = sorted(
            User.objects.filter(is_active=True)
            .annotate(_match_email=Lower("email"))
            .filter(_match_email__in=emails)
            .values_list("email", flat=True)
        )
        if not live:
            return
        sample = ", ".join(live[:SKIP_SAMPLE_SIZE])
        if len(live) > SKIP_SAMPLE_SIZE:
            sample += f", and {len(live) - SKIP_SAMPLE_SIZE} more"
        self.message_user(
            request,
            f"{len(live)} login account(s) still exist and still work after "
            f"deleting these staff records: {sample}. Deactivate them under "
            "Authentication and Authorization if these people have left.",
            level=messages.WARNING,
        )

    def _provision_one(self, request, staff):
        """Provision a single staff member and report + log the outcome."""
        had_login_before = User.objects.filter(
            email__iexact=(staff.email or "").strip().lower(), is_active=True
        ).exists()
        outcome = provision_staff_member(staff)
        if outcome is ProvisionOutcome.ALREADY_OK:
            return  # Nothing happened; saying so on every save is just noise.
        if outcome.changed:
            self._log_grant(request, staff, outcome, had_login_before)

        # list_editable makes Django call save_model once per changed row, so
        # emitting a banner here directly would produce one per row — 200 rows,
        # 200 banners, with the real summary pushed off the screen. Buffer them
        # and let changelist_view flush a grouped summary instead.
        buffered = getattr(request, "_provision_outcomes", None)
        if buffered is not None:
            buffered.append(((staff.email or "").strip().lower(), outcome))
            return
        level = messages.SUCCESS if outcome.changed else messages.WARNING
        self.message_user(request, describe(staff.email, outcome), level=level)

    def changelist_view(self, request, extra_context=None):
        """Collect per-row provisioning outcomes from a bulk inline save."""
        request._provision_outcomes = []
        response = super().changelist_view(request, extra_context)
        outcomes = request._provision_outcomes
        # Unset first: message_user must not re-buffer into a list nobody flushes.
        request._provision_outcomes = None
        if outcomes:
            self._report_grouped(request, outcomes)
        return response

    def _report_grouped(self, request, outcomes):
        """One banner per distinct reason, with a sample — not one per person."""
        by_reason = defaultdict(list)
        for email, outcome in outcomes:
            by_reason[outcome].append(email)
        for outcome, emails in by_reason.items():
            sample = ", ".join(sorted(emails)[:SKIP_SAMPLE_SIZE])
            if len(emails) > SKIP_SAMPLE_SIZE:
                sample += f", and {len(emails) - SKIP_SAMPLE_SIZE} more"
            self.message_user(
                request,
                f"{len(emails)} × {OUTCOME_MESSAGES[outcome]}: {sample}",
                level=messages.SUCCESS if outcome.changed else messages.WARNING,
            )

    def _log_grant(self, request, staff, outcome, had_login_before):
        """Write the access grant to the admin log.

        Django's LogEntry records the StaffMember edit, but nothing would record
        that a login account and an authorisation row were created — so "who
        gave this person access, and when?" would be unanswerable a week later.
        Same reasoning as the explicit ``log_change`` in
        ``appraisals.admin.move_misplaced_goal_reviews``. Emails and school
        names only; no performance data is involved.
        """
        if outcome is ProvisionOutcome.UPDATED_PROFILE_SCHOOL:
            note = f"Sign-in school link re-pointed to {staff.school}"
        elif had_login_before:
            note = f"Sign-in enabled ({staff.school}) — school link added"
        else:
            note = f"Sign-in enabled ({staff.school}) — login account created"
        self.log_change(request, staff, note)

    @admin.action(
        permissions=["change"],
        description="Give selected staff a login",
    )
    def provision_logins(self, request, queryset):
        """Bulk version of the same rules, for after a CSV import.

        Superuser-gated explicitly as well as by ``permissions=["change"]``:
        Django appends actions without ``allowed_permissions`` unconditionally
        and ``admin_view`` only checks ``is_active and is_staff``, so relying on
        the model permission alone would let any staff user with change rights
        mint working SSO accounts. Same reasoning as
        ``appraisals.admin.move_misplaced_goal_reviews``.
        """
        if not request.user.is_superuser:
            raise PermissionDenied(
                "Setting up staff logins is restricted to administrators."
            )
        # Two queries per staff member, so a whole-trust selection is a slow
        # request. Point at the command rather than silently taking minutes.
        if queryset.count() > BULK_PROVISION_ADVISORY:
            # Phrased for the person actually reading it: the stated user does
            # not have SSH and cannot run a management command.
            self.message_user(
                request,
                f"Setting up more than {BULK_PROVISION_ADVISORY} staff at once "
                "can take a while. If the page times out, do it in smaller "
                "batches or ask your IT support to run it on the server.",
                level=messages.INFO,
            )
        summary = provision_staff_members(queryset)
        if summary.changed_count:
            newly_able = summary.users_created + summary.profiles_created
            parts = [f"{newly_able} of the selected staff can now sign in."]
            if summary.profiles_updated:
                parts.append(
                    f"{summary.profiles_updated} had their school updated."
                )
            if summary.already_ok:
                parts.append(f"{summary.already_ok} already could.")
            self.message_user(request, " ".join(parts), level=messages.SUCCESS)
        else:
            self.message_user(
                request,
                f"No changes needed. {summary.already_ok} of the selected staff "
                "can already sign in.",
                level=messages.INFO,
            )
        # Never skip silently — that was the original bug. But one banner per
        # row would bury the summary under hundreds of messages after an import,
        # so group by reason and name a sample, the same shape as
        # check_readiness._section.
        if summary.skipped:
            self._report_grouped(request, summary.skipped)

    def _set_staff_type(self, request, queryset, value):
        # Bulk reclassify. .update() bypasses save(), which is fine here: save()
        # only normalises emails and never touches staff_type.
        updated = queryset.update(staff_type=value)
        label = StaffMember.StaffType(value).label
        self.message_user(request, f"Set {updated} staff member(s) to {label}.")

    @admin.action(description="Set staff type: Teaching")
    def set_type_teaching(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.TEACHING)

    @admin.action(description="Set staff type: Support")
    def set_type_support(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.SUPPORT)

    @admin.action(description="Set staff type: Senior leader")
    def set_type_leader(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.LEADER)


@admin.register(Branding)
class BrandingAdmin(admin.ModelAdmin):
    list_display = ("__str__",)

"""Turning a StaffMember into a working login.

Identity in this project is by email: there is no FK between ``StaffMember``
and ``auth.User`` (see :mod:`core.identity`). A person can therefore exist as a
``StaffMember`` while having no way to sign in at all. Microsoft SSO needs two
further rows, both created here:

* an active ``User`` whose email matches the staff member's, and
* a ``SchoolProfile`` for that user — the authorisation gate itself
  (see :class:`core.allauth_adapters.RestrictMicrosoftLoginAdapter`).

The rules live in this module rather than in the admin or the management
command so the two front ends, and the ``check_readiness`` audit, cannot drift
apart. It is named for its one job rather than being a general ``services.py``.

Every decision is expressed as a :class:`ProvisionOutcome`, so a caller can
report *why* somebody was skipped instead of silently doing nothing — the
failure mode this module exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from django.contrib.auth.models import User
from django.db import transaction
from django.db import IntegrityError
from django.db.models import Count, Exists, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower

from .models import SchoolProfile, StaffMember


class ProvisionOutcome(Enum):
    """What provisioning did, or why it declined to."""

    CREATED_USER = "created_user"
    CREATED_PROFILE = "created_profile"
    CREATED_BOTH = "created_both"
    UPDATED_PROFILE_SCHOOL = "updated_profile_school"
    ALREADY_OK = "already_ok"
    SKIPPED_NO_EMAIL = "skipped_no_email"
    SKIPPED_NO_SCHOOL = "skipped_no_school"
    SKIPPED_SUPERUSER = "skipped_superuser"
    SKIPPED_INACTIVE_USER = "skipped_inactive_user"
    SKIPPED_DUPLICATE_USER = "skipped_duplicate_user"
    SKIPPED_USERNAME_TAKEN = "skipped_username_taken"

    @property
    def is_skip(self) -> bool:
        return self.name.startswith("SKIPPED_")

    @property
    def changed(self) -> bool:
        """True when rows were written. Derived, so a new member cannot be
        forgotten here — everything that is neither a skip nor a no-op wrote."""
        return not self.is_skip and self is not ProvisionOutcome.ALREADY_OK


#: Human-readable explanations, used by both the admin and the CLI so the
#: wording of a skip is identical wherever it surfaces.
OUTCOME_MESSAGES = {
    ProvisionOutcome.CREATED_BOTH: "login account and school link created",
    ProvisionOutcome.CREATED_USER: "login account created",
    ProvisionOutcome.CREATED_PROFILE: "school link created",
    ProvisionOutcome.UPDATED_PROFILE_SCHOOL: "school link updated to the new school",
    ProvisionOutcome.ALREADY_OK: "already able to sign in",
    ProvisionOutcome.SKIPPED_NO_EMAIL: "no email address on the staff record",
    # Worded to fit both front ends: this is shown after a single save *and*
    # after the bulk action, where "save again" would not match what the
    # administrator just did.
    ProvisionOutcome.SKIPPED_NO_SCHOOL: (
        "no school assigned yet — set a school on their staff record to finish "
        "setting up their login"
    ),
    ProvisionOutcome.SKIPPED_SUPERUSER: (
        "is an administrator and already has full access"
    ),
    ProvisionOutcome.SKIPPED_INACTIVE_USER: (
        "their login account is deactivated — reactivate the user in "
        "Authentication and Authorization if they have returned"
    ),
    ProvisionOutcome.SKIPPED_DUPLICATE_USER: (
        "more than one active login account uses this email address — remove the "
        "duplicate before their access can be set up"
    ),
    ProvisionOutcome.SKIPPED_USERNAME_TAKEN: (
        "another login account already uses this email address as its username "
        "but has a different email on file — an administrator needs to merge or "
        "remove that account first"
    ),
}


#: Labels for the "Can sign in?" column. Constants rather than inline literals
#: so the admin's filter options and the column can genuinely not disagree —
#: same idiom as the reason constants in ``appraisals.goal_review_fix``.
LABEL_YES = "Yes"
LABEL_ADMIN = "Administrator"
LABEL_NO_EMAIL = "No — no email address"
LABEL_NO_LOGIN = "No — no login account"
LABEL_NO_SCHOOL_SET = "No — no school on this record"
# Distinct from the above on purpose, and worded so the two cannot be confused:
# this one means the login exists but carries no authorisation row.
LABEL_NO_SCHOOL_LINK = "No — login not linked to a school"
LABEL_DEACTIVATED = "No — login deactivated"
LABEL_DUPLICATE = "No — duplicate login accounts"


@dataclass
class ProvisionSummary:
    """Tallies across a batch, plus per-person reasons for anything skipped."""

    users_created: int = 0
    profiles_created: int = 0
    profiles_updated: int = 0
    already_ok: int = 0
    skipped: list[tuple[str, ProvisionOutcome]] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return self.users_created + self.profiles_created + self.profiles_updated

    def record(self, email: str, outcome: ProvisionOutcome) -> None:
        # Skips are tested first, so a future outcome that nobody adds to the
        # chain below shows up as a visible miscount rather than being silently
        # reported to the operator as a skipped person.
        if outcome.is_skip:
            self.skipped.append((email, outcome))
            return
        if outcome is ProvisionOutcome.CREATED_BOTH:
            self.users_created += 1
            self.profiles_created += 1
        elif outcome is ProvisionOutcome.CREATED_USER:
            self.users_created += 1
        elif outcome is ProvisionOutcome.CREATED_PROFILE:
            self.profiles_created += 1
        elif outcome is ProvisionOutcome.UPDATED_PROFILE_SCHOOL:
            self.profiles_updated += 1
        elif outcome is ProvisionOutcome.ALREADY_OK:
            self.already_ok += 1


def describe(email: str, outcome: ProvisionOutcome) -> str:
    """One line of feedback for a single staff member."""
    return f"{email}: {OUTCOME_MESSAGES[outcome]}."


def provision_staff_member(
    staff: StaffMember, *, dry_run: bool = False
) -> ProvisionOutcome:
    """Ensure ``staff`` has an active User and a SchoolProfile.

    Idempotent, and safe to call on every save: a staff member saved without a
    school is reported as ``SKIPPED_NO_SCHOOL`` and provisioned on a later save
    once the school is set. That retry is the whole point — the previous
    behaviour gave up permanently on a school-less row, so assigning the school
    afterwards left the person unable to log in with nothing to say why.

    Three rules are deliberate and load-bearing:

    * An existing user is **never reactivated**. Deactivating a ``User`` is how
      a leaver's access is revoked, and provisioning must not quietly undo it.
    * Two active users sharing an email is refused rather than resolved. The SSO
      gate picks one of them, so guessing here would decide someone's access by
      accident.
    * Superusers are never touched; they bypass the SchoolProfile gate already.
    """
    email = (staff.email or "").strip().lower()
    if not email:
        return ProvisionOutcome.SKIPPED_NO_EMAIL

    users = list(User.objects.filter(email__iexact=email))
    active = [u for u in users if u.is_active]

    # Only an *active* superuser has full access. Testing every matching user
    # would report a long-deactivated admin account sharing this email as
    # "already has full access", which is false, and the real staff member would
    # never be provisioned. This also keeps the answer identical to the
    # ``_is_admin`` annotation below, which counts active superusers only.
    if any(u.is_superuser for u in active):
        return ProvisionOutcome.SKIPPED_SUPERUSER

    if len(active) > 1:
        return ProvisionOutcome.SKIPPED_DUPLICATE_USER
    if users and not active:
        return ProvisionOutcome.SKIPPED_INACTIVE_USER

    user = active[0] if active else None
    profile = (
        SchoolProfile.objects.filter(user=user).first() if user is not None else None
    )

    # Checked before the school requirement so an already-working login is never
    # reported as a problem just because its StaffMember lost its school FK.
    if profile is not None:
        if staff.school_id and profile.school_id != staff.school_id:
            # Not silent: the caller reports this, because it re-points the row
            # the SSO gate reads. Only ever triggered by someone explicitly
            # changing the school on the StaffMember.
            #
            # The `schools` M2M is deliberately NOT touched here. Adding to it
            # would never remove, so moving someone A -> B -> C would leave them
            # holding all three; nothing reads that field for access today, but
            # CLAUDE.md flags multi-school as coming, and a silent monotonic
            # union is the wrong thing to inherit when it does. Multi-school
            # membership should be a deliberate choice made in SchoolProfileAdmin.
            if not dry_run:
                with transaction.atomic():
                    profile.school = staff.school
                    profile.save(update_fields=["school"])
            return ProvisionOutcome.UPDATED_PROFILE_SCHOOL
        return ProvisionOutcome.ALREADY_OK

    # A SchoolProfile's school FK is mandatory, so without a school there is no
    # valid gate to create. Creating the User alone would leave them denied at
    # login, so report instead and wait for the school.
    if staff.school is None:
        return ProvisionOutcome.SKIPPED_NO_SCHOOL

    username = email[:150]
    if user is None:
        # username is unique, and an account whose username is this email but
        # whose email field is blank or different is invisible to the lookup
        # above — so creating would raise IntegrityError and hand a non-technical
        # administrator a 500 page. Checked rather than caught so --dry-run
        # predicts it too, instead of promising a create that cannot happen.
        if User.objects.filter(username=username).exists():
            return ProvisionOutcome.SKIPPED_USERNAME_TAKEN

    outcome = (
        ProvisionOutcome.CREATED_BOTH
        if user is None
        else ProvisionOutcome.CREATED_PROFILE
    )
    if dry_run:
        return outcome

    try:
        with transaction.atomic():
            if user is None:
                # create_user with no password sets an unusable one: SSO-only, so
                # no local password should ever authenticate this account.
                user = User.objects.create_user(username=username, email=email)
            profile = SchoolProfile.objects.create(user=user, school=staff.school)
            profile.schools.add(staff.school)
    except IntegrityError:
        # The pre-check above closes the ordinary case; this closes the race and
        # the >150-character truncation collision. Caught rather than allowed to
        # propagate because an uncaught IntegrityError escaping save_model would
        # roll back Django's whole changelist POST — every other row in the
        # batch with it — and show a non-technical administrator a 500 page.
        return ProvisionOutcome.SKIPPED_USERNAME_TAKEN
    return outcome


def provision_staff_members(queryset, *, dry_run: bool = False) -> ProvisionSummary:
    """Run :func:`provision_staff_member` across a queryset, tallying outcomes.

    Each staff member is its own transaction (inside the per-member call), so
    one unprovisionable row cannot roll back the rest of a bulk run — the same
    "one row, one unit of work" rule ``data_import`` follows.
    """
    summary = ProvisionSummary()
    for staff in queryset.select_related("school").order_by("email"):
        outcome = provision_staff_member(staff, dry_run=dry_run)
        summary.record((staff.email or "").strip().lower(), outcome)
    return summary


# --- Showing login state in a list ------------------------------------------
#
# These exist so an administrator can *see* who cannot sign in, rather than
# finding out when the person phones. Both flags come from correlated
# subqueries, so a changelist of any size costs a fixed number of queries.

def annotate_login_state(queryset):
    """Attach ``_has_login`` / ``_has_profile`` / ``_is_admin`` to StaffMembers.

    Matched on email, case-insensitively, because that is the only join between
    ``StaffMember`` and ``User`` (see :mod:`core.identity`).

    Deliberately ``Lower()`` on both sides rather than
    ``email__iexact=OuterRef(...)``: ``iexact`` compiles to ``LIKE``, and while
    Django escapes a literal right-hand side it cannot escape a *column*
    reference, so an email containing ``_`` or ``%`` would act as a wildcard and
    match the wrong user. Not hypothetical — ``first_last@school.uk`` is an
    ordinary address. ``LOWER(a) = LOWER(b)`` has no pattern semantics at all.
    """
    active_users = (
        User.objects.filter(is_active=True)
        .annotate(_user_email=Lower("email"))
        .filter(_user_email=OuterRef("_staff_email"))
    )
    inactive_users = (
        User.objects.filter(is_active=False)
        .annotate(_user_email=Lower("email"))
        .filter(_user_email=OuterRef("_staff_email"))
    )
    profiles = (
        SchoolProfile.objects.filter(user__is_active=True)
        .annotate(_user_email=Lower("user__email"))
        .filter(_user_email=OuterRef("_staff_email"))
    )
    # _has_login and _has_profile are independent subqueries, so on their own
    # they would answer "yes" for a person whose profile sits on a *different*
    # duplicate account from the one the SSO gate actually picks — reporting
    # someone as set up who is in fact denied at login. Counting the accounts
    # lets the label call that case out instead of guessing.
    duplicate_logins = (
        User.objects.filter(is_active=True)
        .annotate(_user_email=Lower("email"))
        .filter(_user_email=OuterRef("_staff_email"))
        .order_by()
        .values("_user_email")
        .annotate(n=Count("*"))
        .values("n")
    )
    return queryset.annotate(_staff_email=Lower("email")).annotate(
        _has_login=Exists(active_users),
        _is_admin=Exists(active_users.filter(is_superuser=True)),
        _has_profile=Exists(profiles),
        _has_inactive_login=Exists(inactive_users),
        _login_count=Coalesce(
            Subquery(duplicate_logins, output_field=IntegerField()), Value(0)
        ),
    )


def login_state_label(staff) -> str:
    """Map the annotations from :func:`annotate_login_state` to a plain answer.

    Pure, so it can be unit-tested without a request — the same split
    ``overview.classify()`` uses.
    """
    if not (staff.email or "").strip():
        return LABEL_NO_EMAIL
    if getattr(staff, "_is_admin", False):
        return LABEL_ADMIN
    # Before "no login": duplicates would otherwise read as a confident "Yes"
    # while the gate denies them, which is the one wrong answer an administrator
    # would act on.
    if getattr(staff, "_login_count", 0) > 1:
        return LABEL_DUPLICATE
    if not getattr(staff, "_has_login", False):
        # A deactivated account is a deliberate revocation, not an oversight.
        # Saying "no login account" here invites an admin to "fix" it and hand a
        # leaver their access back.
        if getattr(staff, "_has_inactive_login", False):
            return LABEL_DEACTIVATED
        # Name the *actionable* problem. Someone with no school cannot be given
        # a login until one is set, so reporting "no login account" sends the
        # administrator to the bulk action, which then refuses — two steps to
        # learn what this cell could have said. This is the exact case that
        # prompted the whole feature, so it must not be the vague one.
        if staff.school_id is None:
            return LABEL_NO_SCHOOL_SET
        return LABEL_NO_LOGIN
    if not getattr(staff, "_has_profile", False):
        return LABEL_NO_SCHOOL_LINK
    return LABEL_YES

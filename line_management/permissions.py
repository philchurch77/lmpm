"""Identity and access-control helpers for the line_management app.

Identity is by email (a Django ``User`` is matched to a ``StaffMember`` by a
case-insensitive email compare via ``core.identity.current_staff_member``).

**Authorization is a live lookup.** A viewer is the "manager" of a meeting when
their email matches the meeting's staff member's *current* ``line_manager_email``
— recomputed on every request, never snapshotted. A successor line manager
therefore inherits read+edit of the full history and a former manager loses
access. ``LineMeeting.created_by_email`` preserves who actually authored each
record for display, so inherited notes stay attributed to their author.

Every detail/save view routes through ``get_meeting_or_403`` (and the manager-only
views through ``get_managed_staff_or_403``) so a guessed primary key is denied
rather than leaked — the IDOR chokepoint.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from core.identity import current_staff_member  # re-exported for this app's callers
from core.models import StaffMember

from .models import LineMeeting

# Role names returned by meeting_role().
ROLE_SUPER = "super"
ROLE_MANAGER = "manager"
ROLE_REPORT = "report"
ROLE_NONE = "none"


def line_managed_staff(staff):
    """StaffMembers whose line manager is ``staff`` (the people they line-manage)."""
    if staff is None:
        return StaffMember.objects.none()
    return (
        StaffMember.objects.filter(line_manager_email__iexact=staff.email)
        .exclude(pk=staff.pk)
        .order_by("email")
    )


def is_line_manager(staff) -> bool:
    """Whether ``staff`` line-manages anyone (drives the My Reports nav item)."""
    return line_managed_staff(staff).exists()


def is_current_line_manager(member, staff) -> bool:
    """Whether ``staff`` is ``member``'s current line manager (the access rule).

    The single source of truth for the live line-manager comparison; both
    ``meeting_role`` and ``get_managed_staff_or_403`` defer to it so the security
    boundary can never drift between "what's my role" and "may I act on them".
    """
    return bool(
        staff is not None
        and member.line_manager_email
        # StaffMember.save() already lowercases stored emails, so this .lower()
        # is belt-and-braces: it keeps the comparison correct when an email
        # reaches the DB bypassing save() (bulk import / raw SQL), or when the
        # logged-in user's email case differs from the stored StaffMember email.
        and member.line_manager_email.lower() == (staff.email or "").lower()
    )


def meeting_role(meeting, staff, user) -> str:
    """The viewer's role for a specific meeting (live lookup, see module docstring).

    Relies on ``meeting.staff`` being select_related (see ``get_meeting_or_403``).
    """
    if user.is_superuser:
        return ROLE_SUPER
    if staff is not None and meeting.staff_id == staff.pk:
        return ROLE_REPORT
    if is_current_line_manager(meeting.staff, staff):
        return ROLE_MANAGER
    return ROLE_NONE


def _can_edit_meeting(role) -> bool:
    """The current line manager (or a superuser) may edit every field of a meeting,
    Held or not. The report's narrower rights are ``_can_prepare_meeting``.

    Private on purpose: callers ask ``edit_scope`` (what may be edited) or
    ``can_hold_meeting`` (who may hold), so no view can bypass the scope."""
    return role in {ROLE_MANAGER, ROLE_SUPER}


def _can_prepare_meeting(role, meeting) -> bool:
    """The report may prepare their own meeting while it is being prepared.

    Preparing is the ``REPORT_FIELDS`` of the notes form, plus the carried and
    agreed actions — never the Rotation update, never holding. Once Held, the
    record is read-only to them. An unsaved meeting counts as being prepared.
    """
    return role == ROLE_REPORT and not meeting.is_held


# What the viewer may edit on a meeting page (``edit_scope``).
SCOPE_ALL = "all"  # every field: the line manager or a superuser
SCOPE_PREPARE = "prepare"  # the report's fields, while being prepared
SCOPE_NONE = "none"  # read-only


def edit_scope(role, meeting) -> str:
    """The one answer to "what may this viewer edit on this meeting's page?".

    Every form bind takes it, so a field outside the scope is built ``disabled``
    and never written — however the page was crafted.
    """
    if _can_edit_meeting(role):
        return SCOPE_ALL
    if _can_prepare_meeting(role, meeting):
        return SCOPE_PREPARE
    return SCOPE_NONE


def why_cannot_prepare(staff):
    """Why ``staff`` may not start preparing their own next meeting, or ``None`` if they may.

    The one rule, worded for the person and for whoever fixes their record.
    They need a line manager recorded who is not themselves: someone has to be
    able to complete the record and hold it. (Were ``line_manager_email`` their
    own address, ``meeting_role`` would read them as the report, and nobody but a
    superuser could ever hold the meeting.) Each case gets its own wording, so an
    administrator is sent to the actual fault.
    """
    if staff is None:
        return "There is no staff record for you, so you can't prepare a line meeting."
    if not staff.line_manager_email:
        return (
            "No line manager is recorded for you, so you can't prepare a meeting yet. "
            "Ask your administrator to record your line manager."
        )
    if staff.line_manager_email.lower() == (staff.email or "").lower():
        return (
            "Your staff record names you as your own line manager, so you can't prepare a "
            "meeting yet. Ask your administrator to record your actual line manager."
        )
    return None


def may_start_preparing(staff) -> bool:
    """Whether ``staff`` may start preparing their own next meeting (``why_cannot_prepare``)."""
    return why_cannot_prepare(staff) is None


def can_hold_meeting(role) -> bool:
    """Only the current line manager (or a superuser) may mark a meeting Held.

    Deliberately separate from ``edit_scope``: leg 4 lets the report edit a
    meeting being prepared, but holding it stays the line manager's call.
    """
    return role in {ROLE_MANAGER, ROLE_SUPER}


def get_meeting_or_403(request, pk):
    """Fetch a meeting and the viewer's role, or raise 403.

    Single chokepoint for the meeting detail/save views: denies access (rather
    than 404) when the viewer has no role, preventing IDOR via guessed keys.
    """
    meeting = get_object_or_404(
        LineMeeting.objects.select_related("staff"), pk=pk
    )
    staff = current_staff_member(request)
    role = meeting_role(meeting, staff, request.user)
    if role == ROLE_NONE:
        raise PermissionDenied("You do not have access to this meeting record.")
    return meeting, staff, role


def get_own_staff_to_prepare_or_403(request):
    """The viewer's own ``StaffMember``, if they may prepare their next meeting, or 403.

    Chokepoint for the report's prepare views. The person is always the viewer —
    nothing is read from the URL or the POST — so there is no id to guess.
    """
    staff = current_staff_member(request)
    reason = why_cannot_prepare(staff)
    if reason is not None:
        raise PermissionDenied(reason)
    return staff


def get_managed_staff_or_403(request, staff_pk):
    """Fetch a staff member the viewer line-manages, or raise 403.

    Chokepoint for the manager-only views (per-person list, create): a viewer may
    only act on someone they currently line-manage (superusers bypass).
    """
    member = get_object_or_404(StaffMember, pk=staff_pk)
    staff = current_staff_member(request)
    if request.user.is_superuser or is_current_line_manager(member, staff):
        return member, staff
    raise PermissionDenied("You do not line-manage this person.")

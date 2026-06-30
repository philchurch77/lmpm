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


def can_edit_meeting(role) -> bool:
    """Only the current line manager (or a superuser) may edit a meeting."""
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

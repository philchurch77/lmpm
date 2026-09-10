"""Identity and access-control helpers for the appraisals app.

Identity is by email: a Django ``User`` is matched to a ``StaffMember`` via a
case-insensitive email compare (there is no foreign key between them). These are
plain helpers, not Django permissions.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from core.identity import current_staff_member
from core.models import StaffMember

from .models import Appraisal

# Role names returned by appraisal_role().
ROLE_SUPER = "super"
ROLE_TEACHER = "teacher"
ROLE_COACH = "coach"
ROLE_NONE = "none"


def coached_staff(staff):
    """StaffMembers whose performance manager is ``staff`` (the people they coach)."""
    if staff is None:
        return StaffMember.objects.none()
    return (
        StaffMember.objects.filter(performance_manager_email__iexact=staff.email)
        .exclude(pk=staff.pk)
        .order_by("email")
    )


def is_coach(staff) -> bool:
    """Whether ``staff`` performance-manages anyone (drives the My Team nav item)."""
    return coached_staff(staff).exists()


def appraisal_role(appraisal, staff, user) -> str:
    """The viewer's role for a specific appraisal."""
    if user.is_superuser:
        return ROLE_SUPER
    if staff is not None and appraisal.teacher_id == staff.pk:
        return ROLE_TEACHER
    if (
        staff is not None
        and appraisal.coach_email
        and appraisal.coach_email == staff.email
    ):
        return ROLE_COACH
    return ROLE_NONE


# The role-only halves of the edit gates, split out from the lock so a refused
# save can tell the two reasons apart. "You are not the teacher" is a genuine
# permission denial; "you are the teacher but this was signed off while you were
# typing" is a conflict, and the caller hands the user's text back rather than
# discarding it behind a 403. Each gate carries its role-only half as
# ``.role_only`` so views can ask the question without a second lookup table
# that could drift out of step with the gate itself.
def has_teacher_role(appraisal, role) -> bool:
    return role in {ROLE_TEACHER, ROLE_SUPER}


def has_coach_role(appraisal, role) -> bool:
    return role in {ROLE_COACH, ROLE_SUPER}


def can_edit_teacher_fields(appraisal, role) -> bool:
    return has_teacher_role(appraisal, role) and not appraisal.is_locked


def can_edit_coach_fields(appraisal, role) -> bool:
    return has_coach_role(appraisal, role) and not appraisal.is_locked


can_edit_teacher_fields.role_only = has_teacher_role
can_edit_coach_fields.role_only = has_coach_role


def get_appraisal_or_403(request, pk):
    """Fetch an appraisal and the viewer's role, or raise 403.

    Single chokepoint for every detail/save view: denies access (rather than
    404) when the viewer has no role on the appraisal, preventing IDOR via
    guessed primary keys.
    """
    appraisal = get_object_or_404(
        Appraisal.objects.select_related("teacher", "academic_year"), pk=pk
    )
    staff = current_staff_member(request)
    role = appraisal_role(appraisal, staff, request.user)
    if role == ROLE_NONE:
        raise PermissionDenied("You do not have access to this goal setting and review.")
    return appraisal, staff, role

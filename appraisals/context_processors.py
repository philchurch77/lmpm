"""Template context for appraisal navigation.

Exposes ``user_is_coach`` so the sidebar can conditionally show the "My Team"
link. Safe for anonymous users and users with no StaffMember record.
"""
from __future__ import annotations

from core.identity import current_staff_member

from .permissions import is_coach


def appraisal_nav(request):
    try:
        staff = current_staff_member(request)
        return {"user_is_coach": is_coach(staff) if staff else False}
    except Exception:
        # Navigation must never break page rendering.
        return {"user_is_coach": False}

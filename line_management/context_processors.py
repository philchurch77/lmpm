"""Template context for line-management navigation.

Exposes ``user_is_line_manager`` so the sidebar can conditionally show the
"My Reports" link. Safe for anonymous users and users with no StaffMember record.
"""
from __future__ import annotations

from core.identity import current_staff_member

from .permissions import is_line_manager


def line_nav(request):
    try:
        staff = current_staff_member(request)
        return {"user_is_line_manager": is_line_manager(staff) if staff else False}
    except Exception:
        # Navigation must never break page rendering.
        return {"user_is_line_manager": False}

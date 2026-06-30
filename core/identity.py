"""Shared identity resolution for feature apps.

Identity in this project is by email: a Django ``User`` is matched to a
``StaffMember`` via a case-insensitive email compare (there is no foreign key
between them). This helper is used by every feature app (appraisals,
line_management, ...) so the mapping lives in one place rather than being copied
per app.
"""
from __future__ import annotations

from .models import StaffMember


def current_staff_member(request):
    """The StaffMember matching the logged-in user's email, or None.

    Cached on the request to avoid repeat queries within a single request.
    """
    if not request.user.is_authenticated:
        return None
    cached = getattr(request, "_staff_member", "unset")
    if cached != "unset":
        return cached
    email = (request.user.email or "").strip()
    staff = (
        StaffMember.objects.filter(email__iexact=email).first() if email else None
    )
    request._staff_member = staff
    return staff

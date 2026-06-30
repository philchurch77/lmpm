"""The unified "My Team" overview.

A single read-only page that lists every person the signed-in user manages — the
union of the people they *performance-manage* (coach) and the people they
*line-manage* — each shown once with their role(s) and role-appropriate links.

This app owns no models. It composes the existing, already-access-gated query
helpers from the appraisals and line-management apps rather than re-deriving the
email-matching rules:

- ``appraisals.permissions.coached_staff`` (``performance_manager_email``)
- ``line_management.permissions.line_managed_staff`` (``line_manager_email``)

The per-row "Open" links point at the existing detail/list views, which keep
their own ``get_*_or_403`` IDOR chokepoints — this page adds no new access path.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max
from django.shortcuts import render

from core.identity import current_staff_member

from appraisals.models import Appraisal
from appraisals.permissions import coached_staff
from line_management.permissions import line_managed_staff


@login_required
def my_team(request):
    """List everyone the signed-in user manages, in one place."""
    staff = current_staff_member(request)
    if staff is None:
        return render(request, "team/no_staff.html")

    # People I coach (performance-manage) + their current-year appraisal.
    coached = {m.pk: m for m in coached_staff(staff)}
    appraisals = {
        a.teacher_id: a
        for a in Appraisal.objects.filter(
            teacher__in=coached.values(), academic_year__is_current=True
        ).select_related("academic_year")
    }

    # People I line-manage + a meeting summary.
    managed = {
        m.pk: m
        for m in line_managed_staff(staff).annotate(
            meeting_count=Count("line_meetings"),
            last_meeting=Max("line_meetings__meeting_date"),
        )
    }

    # One row per distinct person across both relationships.
    rows = []
    for pk in coached.keys() | managed.keys():
        managed_member = managed.get(pk)
        member = managed_member or coached[pk]
        rows.append(
            {
                "member": member,
                "is_coach": pk in coached,
                "is_manager": pk in managed,
                "appraisal": appraisals.get(pk),
                "meeting_count": getattr(managed_member, "meeting_count", 0),
                "last_meeting": getattr(managed_member, "last_meeting", None),
            }
        )
    rows.sort(key=lambda r: r["member"].email)

    return render(request, "team/my_team.html", {"rows": rows})

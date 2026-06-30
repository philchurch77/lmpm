"""Superuser overview pages.

The home for trust-wide, superuser-only dashboards that compose data from the
feature apps. Two pages so far: appraisal status and line-management engagement.

This app owns no models. Like the ``team`` app, it composes the feature apps
rather than re-deriving their rules. The rule for this family of pages: if a
future overview needs *who-can-see-what* logic, it must call the relevant app's
permissions helper (e.g. ``appraisals.permissions``) — never rebuild the
email-matching access logic here. These pages have no such logic: each is a flat
superuser-only read across *every* StaffMember, so a direct ORM query is fine.

Scope decision (deliberate): the overviews are **trust-wide** — all schools, and
they include staff with missing data (no appraisal / no line manager / no login
account). That breadth is the point: the pages exist to surface who has *not*
engaged. A future *school-scoped* overview must not assume it can reuse these
views unchanged. (The optional school/email filters narrow the *view*, not the
underlying scope.)

The per-row "Open" links point at the existing ``appraisals:detail`` and
``line_management:staff_meetings`` views. Those drill-ins are safe not because
they have their own superuser gate, but because their ``get_*_or_403``
chokepoints treat a superuser as ``ROLE_SUPER`` and so never return
``ROLE_NONE`` — i.e. the security lives in those chokepoints, and these pages add
no new read path.
"""
from __future__ import annotations

from collections import Counter

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max
from django.shortcuts import render

from core.models import School, StaffMember

from appraisals.models import AcademicYear, Appraisal


def _require_superuser(request):
    """Single gate for every overview view: superuser-only, 403 otherwise."""
    if not request.user.is_superuser:
        raise PermissionDenied("This overview is restricted to administrators.")


def _filter_staff(request, base_qs):
    """Apply the shared school/email filters to a StaffMember queryset.

    Returns ``(queryset, filter_context)``. Both filters come from GET and narrow
    the rows server-side, so the headline counts (derived from the rows) reflect
    the filtered view. A bad/absent school id is ignored rather than erroring. The
    returned context feeds the shared ``overview/_filters.html`` partial.
    """
    school_id = request.GET.get("school", "").strip()
    query = request.GET.get("q", "").strip()
    valid_school_id = school_id if school_id.isdigit() else ""

    qs = base_qs
    if valid_school_id:
        qs = qs.filter(school_id=valid_school_id)
    if query:
        qs = qs.filter(email__icontains=query)

    filter_context = {
        "schools": School.objects.order_by("name"),
        "selected_school": valid_school_id,
        "query": query,
        "is_filtered": bool(valid_school_id or query),
    }
    return qs, filter_context


def classify(member, appraisal):
    """Map a staff member + their current-year appraisal (or None) to a status.

    Returns ``(state, label)`` where ``state`` is the CSS-class suffix and the
    completion bucket. The precedence is a real business rule: a *signed-off*
    appraisal is complete; any other appraisal (DRAFT/SHARED) is in progress;
    with no appraisal, a blank ``staff_type`` is a data-prep gap ("not
    classified") that takes priority over "not started" so it reads as a setup
    problem, not a person to chase. A pure function so it can be unit-tested.
    """
    if appraisal is not None:
        if appraisal.is_locked:  # is_locked == signed off
            return "signed_off", "Signed off"
        return "in_progress", "In progress"
    if not member.staff_type:
        return "not_classified", "Not classified"
    return "not_started", "Not started"


def classify_line(member, meeting_count):
    """Map a staff member + their line-meeting count to a status.

    Returns ``(state, label)``. Line meetings are recurring with no status field,
    so this reflects engagement, not completion: a blank ``line_manager_email`` is
    a data-prep gap ("No manager") that takes priority — like "not classified" on
    the appraisals page — because such a person can have no meetings by design;
    otherwise it's simply whether any meeting has been recorded. A pure function
    so it can be unit-tested.
    """
    if not member.line_manager_email:
        return "no_manager", "No manager"
    if meeting_count == 0:
        return "no_meetings", "No meetings"
    return "has_meetings", "Has meetings"


@login_required
def appraisals_overview(request):
    """Superuser-only: every staff member's current-year appraisal status."""
    _require_superuser(request)

    year = AcademicYear.objects.filter(is_current=True).first()
    if year is None:
        return render(request, "overview/appraisals.html", {"no_year": True})

    # One query for the whole year's appraisals, keyed by teacher. The
    # unique_appraisal_per_teacher_year constraint guarantees at most one per
    # teacher, so this dict never silently drops a row.
    appraisals = {
        a.teacher_id: a
        for a in Appraisal.objects.filter(academic_year=year)
    }

    members, filter_context = _filter_staff(
        request, StaffMember.objects.select_related("school").order_by("email")
    )

    rows = []
    for member in members:
        appraisal = appraisals.get(member.pk)
        state, label = classify(member, appraisal)
        rows.append(
            {
                "member": member,
                "appraisal": appraisal,
                "state": state,
                "label": label,
            }
        )

    # Single source of truth: counts are derived from the rows, never tracked in
    # parallel. Counter returns 0 for any state absent from the rows.
    counts = Counter(row["state"] for row in rows)

    context = {
        "year": year,
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        **filter_context,
    }
    return render(request, "overview/appraisals.html", context)


@login_required
def line_management_overview(request):
    """Superuser-only: every staff member's line-meeting engagement."""
    _require_superuser(request)

    # Per-staff meeting count + last-meeting date in one annotated query (no
    # N+1), exactly as the team page does.
    base_qs = (
        StaffMember.objects.select_related("school")
        .annotate(
            meeting_count=Count("line_meetings"),
            last_meeting=Max("line_meetings__meeting_date"),
        )
        .order_by("email")
    )
    members, filter_context = _filter_staff(request, base_qs)

    rows = []
    for member in members:
        state, label = classify_line(member, member.meeting_count)
        rows.append(
            {
                "member": member,
                "meeting_count": member.meeting_count,
                "last_meeting": member.last_meeting,
                "state": state,
                "label": label,
            }
        )

    counts = Counter(row["state"] for row in rows)

    context = {
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        **filter_context,
    }
    return render(request, "overview/line_management.html", context)

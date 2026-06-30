"""Views for the line-management meeting UI.

All views are function-based and require login. Access to a specific meeting
always routes through ``get_meeting_or_403`` (and manager-only actions through
``get_managed_staff_or_403``) to prevent IDOR, and field-level editing is gated
inside ``LineMeetingForm`` by the ``can_edit`` flag.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.identity import current_staff_member

from .forms import LineMeetingForm
from .models import ROTATION_GUIDANCE, LineMeeting
from .permissions import (
    ROLE_MANAGER,
    ROLE_SUPER,
    can_edit_meeting,
    get_managed_staff_or_403,
    get_meeting_or_403,
    line_managed_staff,
)


@login_required
def my_meetings(request):
    """The signed-in user's line-meeting records.

    Two sections: meetings about the user themselves (read-only, as the report)
    and meetings for the people they *currently* line-manage (editable). The
    second list uses the same live line-manager lookup as the access rule, so
    every meeting shown is one the user can open and edit right now.
    """
    staff = current_staff_member(request)
    if staff is None:
        return render(request, "line_management/no_staff.html")

    own_meetings = LineMeeting.objects.filter(staff=staff)
    hosted_meetings = LineMeeting.objects.filter(
        staff__in=line_managed_staff(staff)
    ).select_related("staff")
    return render(
        request,
        "line_management/my_meetings.html",
        {"meetings": own_meetings, "hosted_meetings": hosted_meetings},
    )


@login_required
def staff_meetings(request, staff_pk):
    """One line-managed person's meetings, with a New meeting action."""
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    meetings = LineMeeting.objects.filter(staff=member)
    return render(
        request,
        "line_management/staff_meetings.html",
        {"member": member, "meetings": meetings},
    )


@login_required
def meeting_new(request, staff_pk):
    """Render a blank meeting form for a line-managed person.

    Nothing is persisted here — the record is only written when the manager
    submits the form (see ``meeting_create``), so abandoning a "New meeting"
    click no longer leaves an empty record behind.
    """
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    form = LineMeetingForm(
        instance=LineMeeting(staff=member, meeting_date=timezone.localdate()),
        can_edit=True,
    )
    return _render_new(request, member, form)


@login_required
@require_POST
def meeting_create(request, staff_pk):
    """Persist a new meeting from the submitted form (create-on-save)."""
    member, _staff = get_managed_staff_or_403(request, staff_pk)
    meeting = LineMeeting(staff=member, created_by_email=request.user.email or "")
    form = LineMeetingForm(request.POST, instance=meeting, can_edit=True)
    if form.is_valid():
        # is_valid() applies the cleaned values to form.instance, so this reflects
        # what was submitted. Refuse to persist a notes-free record (a date alone
        # is not a meeting) — the original reason empty rows used to accrue.
        if meeting.is_empty:
            messages.error(request, "Add at least one note before saving the meeting.")
            return _render_new(request, member, form)
        form.save()
        messages.success(request, "Meeting saved.")
        return redirect("line_management:meeting_detail", pk=meeting.pk)

    messages.error(request, "Please correct the errors below.")
    return _render_new(request, member, form)


def _render_meeting_form(request, *, meeting, member, role, can_edit, form, form_action, is_new):
    context = {
        "meeting": meeting,
        "member": member,
        "role": role,
        "can_edit": can_edit,
        "form": form,
        "form_action": form_action,
        "is_new": is_new,
        "rotation_guidance": ROTATION_GUIDANCE,
    }
    return render(request, "line_management/meeting_detail.html", context)


def _render_new(request, member, form):
    """Render the create form. Only managers/superusers reach the create views."""
    role = ROLE_SUPER if request.user.is_superuser else ROLE_MANAGER
    return _render_meeting_form(
        request,
        meeting=None,
        member=member,
        role=role,
        can_edit=True,
        form=form,
        form_action=reverse("line_management:meeting_create", args=[member.pk]),
        is_new=True,
    )


@login_required
def meeting_detail(request, pk):
    meeting, _staff, role = get_meeting_or_403(request, pk)
    form = LineMeetingForm(instance=meeting, can_edit=can_edit_meeting(role))
    return _render_meeting_form(
        request,
        meeting=meeting,
        member=meeting.staff,
        role=role,
        can_edit=can_edit_meeting(role),
        form=form,
        form_action=reverse("line_management:meeting_save", args=[meeting.pk]),
        is_new=False,
    )


@login_required
@require_POST
def meeting_save(request, pk):
    meeting, _staff, role = get_meeting_or_403(request, pk)
    if not can_edit_meeting(role):
        raise PermissionDenied("You may not edit this meeting record.")

    form = LineMeetingForm(request.POST, instance=meeting, can_edit=True)
    if form.is_valid():
        form.save()
        messages.success(request, "Meeting saved.")
        return redirect("line_management:meeting_detail", pk=meeting.pk)

    messages.error(request, "Please correct the errors below.")
    return _render_meeting_form(
        request,
        meeting=meeting,
        member=meeting.staff,
        role=role,
        can_edit=True,
        form=form,
        form_action=reverse("line_management:meeting_save", args=[meeting.pk]),
        is_new=False,
    )

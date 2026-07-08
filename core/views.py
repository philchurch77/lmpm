from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .identity import current_staff_member
from .models import School


@login_required
def home(request):
    """Landing page: a personalised 'get started' page for the signed-in user.

    ``staff`` may be None (a login with no matching StaffMember); the template
    still shows the appraisal/line-meeting calls-to-action, which route to the
    apps' own empty states. Role flags (user_is_coach / user_is_line_manager)
    come from the appraisals/line_management context processors.
    """
    schools = School.objects.all().order_by("name")
    staff = current_staff_member(request)
    return render(request, "core/home.html", {"schools": schools, "staff": staff})

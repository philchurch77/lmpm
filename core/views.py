from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import School


@login_required
def home(request):
    """Landing page. Replace/extend as the line-management features are built."""
    schools = School.objects.all().order_by("name")
    return render(request, "core/home.html", {"schools": schools})

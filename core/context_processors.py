from __future__ import annotations

from .models import Branding


def branding(request):
    return {"branding": Branding.objects.first()}

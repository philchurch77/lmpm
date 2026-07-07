"""URL configuration for the lmpm project."""
from django.contrib import admin
from django.http import Http404
from django.urls import include, path, re_path

from django.conf import settings
from django.conf.urls.static import static


def _blocked(request, *args, **kwargs):
    raise Http404


# Accounts are pre-provisioned (see core/allauth_adapters.py), and identity is
# the user's email, so the allauth endpoints that create accounts or change
# emails must never be reachable. Password reset is blocked too: no email
# backend is configured, and the form would enable account enumeration.
# These overrides sit BEFORE the allauth include so they win URL resolution;
# core/tests.py pins each one against allauth upgrades re-exposing them.
_blocked_account_urls = [
    path('accounts/signup/', _blocked),
    path('accounts/email/', _blocked),
    re_path(r'^accounts/confirm-email/', _blocked),
    re_path(r'^accounts/password/reset/', _blocked),
    path('accounts/3rdparty/', _blocked),
    path('accounts/3rdparty/signup/', _blocked),
    path('accounts/social/signup/', _blocked),
    path('accounts/social/connections/', _blocked),
]

urlpatterns = _blocked_account_urls + [
    path('', include('core.urls')),
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('appraisals/', include('appraisals.urls')),
    path('line-management/', include('line_management.urls')),
    path('team/', include('team.urls')),
    path('overview/', include('overview.urls')),
    path('import/', include('data_import.urls')),
]

if settings.DEBUG or getattr(settings, "SERVE_MEDIA", False):
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

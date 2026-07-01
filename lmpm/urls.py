"""URL configuration for the lmpm project."""
from django.contrib import admin
from django.urls import include, path

from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
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

from django.contrib import admin

from core.admin_mixins import SuperuserOnlyDeleteMixin

from .models import LineMeeting


@admin.register(LineMeeting)
class LineMeetingAdmin(SuperuserOnlyDeleteMixin, admin.ModelAdmin):
    list_display = ("staff", "meeting_date", "created_by_email")
    list_filter = ("meeting_date",)
    search_fields = ("staff__email", "created_by_email")
    autocomplete_fields = ("staff",)
    date_hierarchy = "meeting_date"

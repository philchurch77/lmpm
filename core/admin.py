from django.contrib import admin

from .models import Branding, School, SchoolProfile, StaffMember


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ("name", "phase")
    search_fields = ("name",)


@admin.register(SchoolProfile)
class SchoolProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "school")
    search_fields = ("user__username", "user__email", "school__name")
    autocomplete_fields = ("user", "school")


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "job_title",
        "department",
        "staff_type",
        "school",
        "line_manager_email",
        "performance_manager_email",
    )
    list_filter = ("school", "department", "staff_type")
    search_fields = (
        "email",
        "line_manager_email",
        "performance_manager_email",
        "job_title",
        "department",
    )
    autocomplete_fields = ("school",)


@admin.register(Branding)
class BrandingAdmin(admin.ModelAdmin):
    list_display = ("__str__",)

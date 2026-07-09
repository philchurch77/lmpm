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
    # Edit staff_type inline from the list (per-row dropdown, one Save button).
    # email is the link column, so staff_type is a valid editable field.
    list_editable = ("staff_type",)
    list_filter = ("school", "department", "staff_type")
    search_fields = (
        "email",
        "line_manager_email",
        "performance_manager_email",
        "job_title",
        "department",
    )
    autocomplete_fields = ("school",)
    actions = (
        "set_type_teaching",
        "set_type_support",
        "set_type_leader",
    )

    def _set_staff_type(self, request, queryset, value):
        # Bulk reclassify. .update() bypasses save(), which is fine here: save()
        # only normalises emails and never touches staff_type.
        updated = queryset.update(staff_type=value)
        label = StaffMember.StaffType(value).label
        self.message_user(request, f"Set {updated} staff member(s) to {label}.")

    @admin.action(description="Set staff type: Teaching")
    def set_type_teaching(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.TEACHING)

    @admin.action(description="Set staff type: Support")
    def set_type_support(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.SUPPORT)

    @admin.action(description="Set staff type: Senior leader")
    def set_type_leader(self, request, queryset):
        self._set_staff_type(request, queryset, StaffMember.StaffType.LEADER)


@admin.register(Branding)
class BrandingAdmin(admin.ModelAdmin):
    list_display = ("__str__",)

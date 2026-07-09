from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path

from .models import (
    AcademicYear,
    Appraisal,
    Goal,
    SelfReview,
    SelfReviewBullet,
    SelfReviewItem,
)


@admin.register(AcademicYear)
class AcademicYearAdmin(admin.ModelAdmin):
    list_display = ("__str__", "start_year", "is_current")
    list_filter = ("is_current",)
    search_fields = ("start_year", "label")
    actions = ("set_as_current_year",)
    # Custom changelist template adds the "Start next academic year" button.
    change_list_template = "admin/appraisals/academicyear/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "start-next-year/",
                self.admin_site.admin_view(self.start_next_year_view),
                name="appraisals_academicyear_start_next_year",
            ),
        ]
        return custom + urls

    def start_next_year_view(self, request):
        """One-click: create the next academic year and make it current."""
        year, created = AcademicYear.start_next()
        verb = "Started" if created else "Switched to"
        self.message_user(
            request,
            f"{verb} {year} and made it the current academic year.",
            level=messages.SUCCESS,
        )
        return redirect("admin:appraisals_academicyear_changelist")

    @admin.action(description="Set as current academic year")
    def set_as_current_year(self, request, queryset):
        """Make the single selected year current (save() demotes the rest)."""
        if queryset.count() != 1:
            self.message_user(
                request,
                "Select exactly one year to set as the current academic year.",
                level=messages.ERROR,
            )
            return
        year = queryset.first()
        year.is_current = True
        year.save()
        self.message_user(
            request,
            f"{year} is now the current academic year.",
            level=messages.SUCCESS,
        )


class GoalInline(admin.TabularInline):
    model = Goal
    extra = 0
    fields = (
        "order",
        "goal_type",
        "title",
        "steps_to_success",
        "success_criteria",
        "teacher_review_comment",
        "coach_review_comment",
    )


@admin.register(Appraisal)
class AppraisalAdmin(admin.ModelAdmin):
    list_display = ("teacher", "academic_year", "coach_email", "status")
    list_filter = ("status", "academic_year")
    search_fields = ("teacher__email", "coach_email")
    autocomplete_fields = ("teacher", "academic_year")
    inlines = (GoalInline,)


@admin.register(Goal)
class GoalAdmin(admin.ModelAdmin):
    list_display = ("appraisal", "order", "goal_type")
    list_filter = ("goal_type",)
    search_fields = ("appraisal__teacher__email", "title")
    autocomplete_fields = ("appraisal",)


class SelfReviewItemInline(admin.TabularInline):
    model = SelfReviewItem
    extra = 0
    fields = ("order", "code", "heading", "evidence")


class SelfReviewBulletInline(admin.TabularInline):
    model = SelfReviewBullet
    extra = 0
    fields = ("order", "text", "score")


@admin.register(SelfReview)
class SelfReviewAdmin(admin.ModelAdmin):
    list_display = ("appraisal", "kind")
    list_filter = ("kind",)
    search_fields = ("appraisal__teacher__email",)
    autocomplete_fields = ("appraisal",)
    inlines = (SelfReviewItemInline,)


@admin.register(SelfReviewItem)
class SelfReviewItemAdmin(admin.ModelAdmin):
    list_display = ("self_review", "order", "code", "heading")
    search_fields = ("self_review__appraisal__teacher__email", "code", "heading")
    autocomplete_fields = ("self_review",)
    inlines = (SelfReviewBulletInline,)

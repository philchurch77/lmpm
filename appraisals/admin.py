from django.contrib import admin

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

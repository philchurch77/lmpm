from django.contrib import admin, messages
from django.shortcuts import redirect
from django.template.defaultfilters import linebreaksbr
from django.urls import path
from django.utils.html import escape
from django.utils.safestring import mark_safe

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


# Shared cell styling for the read-only "at a glance" table.
_CELL = "border:1px solid #ccc;padding:6px 9px;vertical-align:top"
_SCORE_COLOURS = {1: "#c0392b", 2: "#b7791f", 3: "#217a3b"}


def _score_cell(score):
    """One <td> for a bullet's score, colour-coded (blank = Not Answered)."""
    if score is None:
        return f"<td style='{_CELL};text-align:center;color:#999'>—</td>"
    colour = _SCORE_COLOURS.get(score, "#333")
    return (
        f"<td style='{_CELL};text-align:center;font-weight:700;"
        f"color:{colour}'>{score}</td>"
    )


def render_self_review_table(self_review):
    """A clean Section / Criterion / Score / Evidence table for a self-review.

    Groups by ``SelfReviewItem`` (one shared Evidence cell spanning the item's
    bullets) so a school admin sees the criterion wording, its 1-3 score, and
    the staff member's comment together — the score lives on the child bullet,
    which the default inlines never surface alongside the evidence.
    """
    rows = [
        "<table style='border-collapse:collapse;width:100%;font-size:13px'>",
        "<thead><tr>",
        f"<th style='{_CELL};background:#f4f4f4;text-align:left'>Section</th>",
        f"<th style='{_CELL};background:#f4f4f4;text-align:left'>Criterion</th>",
        f"<th style='{_CELL};background:#f4f4f4;width:64px'>Score</th>",
        f"<th style='{_CELL};background:#f4f4f4;text-align:left'>"
        "Evidence / comment</th>",
        "</tr></thead><tbody>",
    ]
    for item in self_review.items.prefetch_related("bullets").all():
        bullets = list(item.bullets.all()) or [None]
        span = len(bullets)
        label = escape(item.code)
        if item.heading:
            label += f"<br><span style='color:#666'>{escape(item.heading)}</span>"
        if item.evidence.strip():
            evidence = linebreaksbr(item.evidence)
        else:
            evidence = "<span style='color:#999'>—</span>"
        for index, bullet in enumerate(bullets):
            cells = []
            if index == 0:
                cells.append(
                    f"<td rowspan='{span}' style='{_CELL};font-weight:600'>"
                    f"{label}</td>"
                )
            if bullet is None:
                cells.append(f"<td style='{_CELL};color:#999'>—</td>")
                cells.append(_score_cell(None))
            else:
                cells.append(f"<td style='{_CELL}'>{escape(bullet.text)}</td>")
                cells.append(_score_cell(bullet.score))
            if index == 0:
                cells.append(f"<td rowspan='{span}' style='{_CELL}'>{evidence}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
    rows.append("</tbody></table>")
    return mark_safe("".join(rows))


class SelfReviewItemInline(admin.TabularInline):
    model = SelfReviewItem
    extra = 0
    fields = ("order", "code", "heading", "scores", "evidence")
    readonly_fields = ("order", "code", "heading", "scores")

    @admin.display(description="Score(s)")
    def scores(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        parts = [
            str(b.score) if b.score is not None else "–" for b in obj.bullets.all()
        ]
        return ", ".join(parts) or "—"


class SelfReviewBulletInline(admin.TabularInline):
    model = SelfReviewBullet
    extra = 0
    fields = ("order", "text", "score")
    readonly_fields = ("order", "text")


@admin.register(SelfReview)
class SelfReviewAdmin(admin.ModelAdmin):
    list_display = ("appraisal", "kind")
    list_filter = ("kind",)
    search_fields = ("appraisal__teacher__email",)
    autocomplete_fields = ("appraisal",)
    readonly_fields = ("review_summary",)
    inlines = (SelfReviewItemInline,)

    def get_fieldsets(self, request, obj=None):
        base = [(None, {"fields": ("appraisal", "kind")})]
        if obj is not None:
            base.append(("Review at a glance", {"fields": ("review_summary",)}))
        return base

    @admin.display(description="")
    def review_summary(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        return render_self_review_table(obj)


@admin.register(SelfReviewItem)
class SelfReviewItemAdmin(admin.ModelAdmin):
    list_display = ("self_review", "order", "code", "heading", "scores")
    search_fields = ("self_review__appraisal__teacher__email", "code", "heading")
    autocomplete_fields = ("self_review",)
    inlines = (SelfReviewBulletInline,)

    @admin.display(description="Score(s)")
    def scores(self, obj):
        parts = [
            str(b.score) if b.score is not None else "–" for b in obj.bullets.all()
        ]
        return ", ".join(parts) or "—"

import json
from datetime import datetime

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.defaultfilters import linebreaksbr
from django.urls import path
from django.utils.html import escape
from django.utils.safestring import mark_safe

from .goal_review_fix import apply_plan, build_plan, plan_counts, restrict_to_approved
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
    actions = ("set_as_current_year", "move_misplaced_goal_reviews")
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
        """One-click: create the next academic year and make it current.

        Superuser-only. ``admin_site.admin_view`` checks ``is_staff`` and
        nothing else, so without this any staff user could roll the trust over
        to a new academic year — which changes what every appraisal view shows.
        """
        if not request.user.is_superuser:
            raise PermissionDenied(
                "Starting a new academic year is restricted to administrators."
            )
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

    @admin.action(
        # Keeps the action off the dropdown for a view-only user. This is the
        # UI tidy-up, NOT the security boundary — Django appends any action
        # without `allowed_permissions` unconditionally, and the changelist
        # itself admits view-only users. The explicit check below is the gate.
        permissions=["change"],
        description="Move misplaced goal reviews to the previous year",
    )
    def move_misplaced_goal_reviews(self, request, queryset):
        """Browser front end for the one-off goal-review correction.

        The decision rules live in ``appraisals.goal_review_fix`` and are shared
        with the ``move_prior_year_goal_reviews`` management command. This exists
        because the operator may have no console access, and a correction to live
        performance data should not depend on one.

        Preview first, confirm second. The plan is rebuilt on confirm so the
        edited-since-import guard is re-evaluated against current data, then
        narrowed to the goals the operator actually saw — so the applied set can
        never be larger than the approved one.

        Restricted to superusers. Django's admin gates the changelist on *view*
        permission and appends actions without ``allowed_permissions``
        unconditionally, so without this check a staff user with read-only
        access to Academic years could rewrite goal review commentary across the
        whole trust and download every staff member's comments. Permission on
        AcademicYear is also the wrong model to gate on — the action rewrites
        Appraisal and Goal.
        """
        if not request.user.is_superuser:
            raise PermissionDenied(
                "Correcting goal review data is restricted to administrators."
            )

        if queryset.count() != 1:
            self.message_user(
                request,
                "Select exactly one academic year — the one whose goals wrongly "
                "hold the reviews (e.g. 2025/26).",
                level=messages.ERROR,
            )
            return None

        source_year = queryset.first()
        to_start = source_year.start_year - 1
        plans = build_plan(source_year, to_start)
        counts = plan_counts(plans)

        if request.POST.get("confirm"):
            approved = {
                int(pk) for pk in request.POST.getlist("approved_goal") if pk.isdigit()
            }
            plans, newly_appeared = restrict_to_approved(plans, approved)

            record = apply_plan(plans, to_start)
            record["not_applied_new_since_preview"] = [
                {"teacher_email": email, "goal_order": order}
                for email, order in newly_appeared
            ]

            # A server-side trace of the mutation, so "who changed my review, and
            # when" is answerable from the system rather than from someone's
            # Downloads folder. Deliberately counts and pks only — the commentary
            # itself stays in the operator's download.
            self.log_change(
                request,
                source_year,
                f"Moved {record['moved_goals']} goal review(s) across "
                f"{record['moved_appraisals']} appraisal(s) into "
                f"{record['target_year']}. Source goal pks: "
                + ", ".join(
                    str(g["source_goal_pk"])
                    for a in record["appraisals"]
                    for g in a.get("goals", [])
                ),
            )

            # Returned as a download rather than written to disk: on Azure the
            # working directory is a disposable temp extract, and this file holds
            # named staff performance commentary that should not linger on the
            # server. The record doubles as the run's summary.
            payload = json.dumps(record, indent=2, ensure_ascii=False)
            response = HttpResponse(payload, content_type="application/json")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            response["Content-Disposition"] = (
                f'attachment; filename="goal-review-move-{stamp}.json"'
            )
            return response

        return render(
            request,
            "admin/appraisals/academicyear/move_goal_reviews.html",
            {
                **self.admin_site.each_context(request),
                "title": "Move misplaced goal reviews",
                "source_year": source_year,
                "target_label": f"{to_start}/{str(to_start + 1)[-2:]}",
                "counts": counts,
                "movable": [p for p in plans if p.moves],
                "flagged": [p for p in plans if p.is_flagged],
                "blocked": [p for p in plans if p.blocked],
                "queryset": queryset,
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
            },
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

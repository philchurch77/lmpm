"""Forms and formsets for the appraisal UI.

Field-level editability is gated by role: each form is told whether the current
user may edit teacher fields and/or coach fields, and any field they may not edit
is set ``disabled`` (Django then ignores submitted values for it — the real
security boundary, not template hiding).

Boolean fields are rendered as Yes/No radio groups (styled as segmented pills in
the templates) rather than checkboxes, to match the paper form.
"""
from __future__ import annotations

from django import forms

from .models import (
    Appraisal,
    Goal,
    LeaderGoal,
    LeaderReview,
    LeaderStandard,
    SelfReview,
    SelfReviewBullet,
    SelfReviewItem,
)

YESNO_CHOICES = [("true", "Yes"), ("false", "No")]
TRISTATE_CHOICES = [("", "—")] + YESNO_CHOICES
SCORE_CHOICES = [("", "Not answered"), ("1", "1"), ("2", "2"), ("3", "3")]


def _to_choice(value):
    """Map a bool/int/None to its radio-choice string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _coerce_bool(value):
    return value == "true"


def _coerce_int(value):
    return int(value) if value else None


def _yesno_field(required=False):
    return forms.TypedChoiceField(
        choices=YESNO_CHOICES,
        coerce=_coerce_bool,
        empty_value=False,
        required=required,
        widget=forms.RadioSelect,
    )


def _score_field():
    return forms.TypedChoiceField(
        choices=SCORE_CHOICES,
        coerce=_coerce_int,
        empty_value=None,
        required=False,
        widget=forms.RadioSelect,
    )


def _tristate_field():
    """Yes/No/unanswered radio group backed by a nullable BooleanField."""
    return forms.TypedChoiceField(
        choices=TRISTATE_CHOICES,
        coerce=_coerce_bool,
        empty_value=None,
        required=False,
        widget=forms.RadioSelect,
    )


class RoleGatedForm(forms.ModelForm):
    """ModelForm that disables fields the current role may not edit.

    Subclasses set ``teacher_fields`` / ``coach_fields``. Segmented boolean
    fields are listed so their initial values can be converted to choice strings.
    """

    teacher_fields: tuple = ()
    coach_fields: tuple = ()
    # Fields rendered as Yes/No (or tri-state) radio groups.
    segmented_fields: tuple = ()

    def __init__(self, *args, can_teacher=False, can_coach=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Radio groups compare option values as strings, so convert the initial
        # bool/None coming from the instance into the matching choice string.
        for name in self.segmented_fields:
            if name in self.fields:
                self.initial[name] = _to_choice(self.initial.get(name))
        for name, field in self.fields.items():
            editable = (name in self.teacher_fields and can_teacher) or (
                name in self.coach_fields and can_coach
            )
            if not editable:
                field.disabled = True


class SelfReviewItemForm(RoleGatedForm):
    teacher_fields = ("evidence",)

    class Meta:
        model = SelfReviewItem
        fields = ("evidence",)
        widgets = {"evidence": forms.Textarea(attrs={"rows": 3})}


class SelfReviewBulletForm(RoleGatedForm):
    teacher_fields = ("score",)
    segmented_fields = ("score",)

    score = _score_field()

    class Meta:
        model = SelfReviewBullet
        fields = ("score",)


class SelfReviewForm(RoleGatedForm):
    teacher_fields = (
        "job_summary",
        "level_description",
        "upr_declaration_agreed",
        "signed_name",
        "signed_date",
    )
    segmented_fields = ("upr_declaration_agreed",)

    upr_declaration_agreed = _yesno_field()

    class Meta:
        model = SelfReview
        fields = (
            "job_summary",
            "level_description",
            "upr_declaration_agreed",
            "signed_name",
            "signed_date",
        )
        widgets = {
            "job_summary": forms.Textarea(attrs={"rows": 4}),
            "level_description": forms.Textarea(attrs={"rows": 4}),
            "signed_date": forms.DateInput(attrs={"type": "date"}),
        }


class GoalForm(RoleGatedForm):
    teacher_fields = (
        "title",
        "steps_to_success",
        "success_criteria",
        "teacher_review_comment",
    )
    coach_fields = ("coach_review_comment",)

    class Meta:
        model = Goal
        fields = (
            "title",
            "steps_to_success",
            "success_criteria",
            "teacher_review_comment",
            "coach_review_comment",
        )
        widgets = {
            "title": forms.Textarea(attrs={"rows": 2}),
            "steps_to_success": forms.Textarea(attrs={"rows": 4}),
            "success_criteria": forms.Textarea(attrs={"rows": 3}),
            "teacher_review_comment": forms.Textarea(attrs={"rows": 3}),
            "coach_review_comment": forms.Textarea(attrs={"rows": 3}),
        }


class AppraisalSummaryForm(RoleGatedForm):
    teacher_fields = ("cpd_requirements", "summary_teacher_comment")
    coach_fields = (
        "summary_coach_comment",
        "on_upper_pay_range",
        "self_review_form_completed",
        "engaged_with_professional_growth",
        "coach_supports_pay_award",
        "job_description_review_needed",
        "status",
    )
    segmented_fields = (
        "on_upper_pay_range",
        "self_review_form_completed",
        "engaged_with_professional_growth",
        "job_description_review_needed",
    )

    on_upper_pay_range = _yesno_field()
    self_review_form_completed = _yesno_field()
    engaged_with_professional_growth = _yesno_field()
    job_description_review_needed = _yesno_field()

    class Meta:
        model = Appraisal
        fields = (
            "cpd_requirements",
            "summary_teacher_comment",
            "summary_coach_comment",
            "on_upper_pay_range",
            "self_review_form_completed",
            "engaged_with_professional_growth",
            "coach_supports_pay_award",
            "job_description_review_needed",
            "status",
        )
        widgets = {
            "cpd_requirements": forms.Textarea(attrs={"rows": 3}),
            "summary_teacher_comment": forms.Textarea(attrs={"rows": 3}),
            "summary_coach_comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Friendlier blank label for the pay-award dropdown.
        if "coach_supports_pay_award" in self.fields:
            self.fields["coach_supports_pay_award"].choices = [
                ("", "Select response")
            ] + list(Appraisal.PayAward.choices)


# Formsets over the pre-seeded child rows (no add/delete in the UI).
GoalFormSet = forms.inlineformset_factory(
    Appraisal, Goal, form=GoalForm, extra=0, can_delete=False
)

SelfReviewItemFormSet = forms.inlineformset_factory(
    SelfReview, SelfReviewItem, form=SelfReviewItemForm, extra=0, can_delete=False
)

# Flat formset over every bullet belonging to one self-review (queryset bound
# explicitly by the view, since SelfReviewBullet's parent is SelfReviewItem,
# not SelfReview, and inlineformset_factory only supports one FK hop).
SelfReviewBulletFormSet = forms.modelformset_factory(
    SelfReviewBullet, form=SelfReviewBulletForm, extra=0, can_delete=False
)


# --- Senior-leader (Headteacher Standards) self-review ---------------------


class LeaderStandardForm(RoleGatedForm):
    """One of the 10 Headteachers' Standards: whole-standard score, a "Not in
    Job Role" toggle and a free-text Examples box (all reviewee-owned).

    `title`/`descriptors` are read-only prompt text, rendered from the instance
    rather than as editable fields.
    """

    teacher_fields = ("score", "not_applicable", "examples")
    segmented_fields = ("score", "not_applicable")

    score = _score_field()
    not_applicable = _yesno_field()

    class Meta:
        model = LeaderStandard
        fields = ("score", "not_applicable", "examples")
        widgets = {"examples": forms.Textarea(attrs={"rows": 3})}


class LeaderGoalForm(RoleGatedForm):
    """A single free-form leader goal (added/removed in the UI)."""

    teacher_fields = ("goal", "evidence_and_discussion", "achieved")
    segmented_fields = ("achieved",)

    achieved = _tristate_field()

    class Meta:
        model = LeaderGoal
        fields = ("goal", "evidence_and_discussion", "achieved")
        widgets = {
            "goal": forms.Textarea(attrs={"rows": 3}),
            "evidence_and_discussion": forms.Textarea(attrs={"rows": 3}),
        }


# Pre-seeded 10 standards (no add/delete). Score lives on the standard itself,
# so a single inline formset off LeaderReview suffices (no bullet second hop).
LeaderStandardFormSet = forms.inlineformset_factory(
    LeaderReview, LeaderStandard, form=LeaderStandardForm, extra=0, can_delete=False
)

# Free-form goals: add/remove rows in the UI.
LeaderGoalFormSet = forms.inlineformset_factory(
    LeaderReview, LeaderGoal, form=LeaderGoalForm, extra=1, can_delete=True
)

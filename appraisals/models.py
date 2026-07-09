from datetime import date

from django.db import models

from core.models import StaffMember

from .leader_standards_templates import HEADTEACHER_STANDARDS
from .self_review_templates import SUPPORT_ITEMS, TEACHING_ITEMS


# Default wording for the fixed "Standards & policies" goal (Goal 1 on the
# Copleston form). Kept as a module constant for now; per-school goal templates
# can override this later without restructuring the models.
DEFAULT_STANDARDS_GOAL = (
    "To ensure the teacher standards are fully met and that school-specific "
    "policies and practices, related to these standards, are adhered to."
)


class AcademicYear(models.Model):
    """An academic year, used to split current vs previous appraisals.

    `start_year` is the calendar year the academic year begins (e.g. 2025 for
    2025/26). It drives ordering and the previous-year lookup.
    """

    start_year = models.PositiveIntegerField(unique=True)
    label = models.CharField(max_length=20, blank=True, default="")
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-start_year"]

    def save(self, *args, **kwargs):
        # Ensure only one year is ever marked current.
        if self.is_current:
            AcademicYear.objects.exclude(pk=self.pk).filter(is_current=True).update(
                is_current=False
            )
        super().save(*args, **kwargs)

    @classmethod
    def start_next(cls):
        """Create (if needed) the year after the current one and make it current.

        Advances the trust to the next academic year in one step: the new year's
        ``save()`` demotes whatever was previously current. Anchored on the
        *current* year (not merely the latest), so a pre-created future year is
        activated rather than skipped or duplicated. Falls back to the latest
        year + 1, or the current calendar year when the table is empty.

        Returns ``(year, created)``.
        """
        current = cls.objects.filter(is_current=True).first()
        if current:
            next_start = current.start_year + 1
        else:
            latest = cls.objects.order_by("-start_year").first()
            next_start = latest.start_year + 1 if latest else date.today().year
        year, created = cls.objects.get_or_create(start_year=next_start)
        year.is_current = True
        year.save()
        return year, created

    def __str__(self):
        if self.label:
            return self.label
        return f"{self.start_year}/{str(self.start_year + 1)[-2:]}"


class Appraisal(models.Model):
    """A teacher's annual appraisal, run with their coach (performance manager).

    There is at most one appraisal per teacher per academic year. The previous
    year's appraisal supplies the read-only "Last Year" section of the form.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SHARED = "SHARED", "Shared"
        SIGNED_OFF = "SIGNED_OFF", "Signed off"

    class PayAward(models.TextChoices):
        # Blank ("") is the unselected "Select response" state.
        YES = "YES", "Yes"
        NO = "NO", "No"
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"

    teacher = models.ForeignKey(
        StaffMember,
        on_delete=models.PROTECT,
        related_name="appraisals",
    )
    academic_year = models.ForeignKey(
        AcademicYear,
        on_delete=models.PROTECT,
        related_name="appraisals",
    )
    # Snapshot of the teacher's performance manager when the appraisal is
    # created, so the historical record is stable if the manager later changes.
    coach_email = models.EmailField(blank=True, default="")

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    signed_off_at = models.DateTimeField(null=True, blank=True)

    # Summary section of the form.
    cpd_requirements = models.TextField(blank=True, default="")
    summary_teacher_comment = models.TextField(blank=True, default="")
    summary_coach_comment = models.TextField(blank=True, default="")

    # Eligibility / engagement checks (toggles on the form).
    on_upper_pay_range = models.BooleanField(default=False)
    self_review_form_completed = models.BooleanField(default=False)
    engaged_with_professional_growth = models.BooleanField(default=False)

    # Coach decisions.
    coach_supports_pay_award = models.CharField(
        max_length=20,
        choices=PayAward.choices,
        blank=True,
        default="",
    )
    # Coach's yes/no toggle.
    job_description_review_needed = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-academic_year__start_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "academic_year"],
                name="unique_appraisal_per_teacher_year",
            ),
        ]

    def save(self, *args, **kwargs):
        self.coach_email = self.coach_email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def is_locked(self) -> bool:
        """Once signed off, the appraisal is read-only."""
        return self.status == self.Status.SIGNED_OFF

    def previous(self):
        """The same teacher's appraisal for the prior academic year, or None.

        Drives the "Last Year" section of the form.
        """
        return (
            Appraisal.objects.filter(
                teacher=self.teacher,
                academic_year__start_year=self.academic_year.start_year - 1,
            )
            .select_related("academic_year")
            .first()
        )

    def seed_goals(self):
        """Create the standard set of goal rows for this appraisal.

        Goal 1 (Standards) is prefilled with the default wording; Goals 2 and 3
        are blank for the teacher to complete. Goal 3 (Leadership/UPR) applies
        only to leaders/UPR staff and may be left blank otherwise. No-op if
        goals already exist. Called from the create view, not from save().
        """
        if self.goals.exists():
            return
        Goal.objects.bulk_create(
            [
                Goal(
                    appraisal=self,
                    goal_type=Goal.GoalType.STANDARDS,
                    order=1,
                    title=DEFAULT_STANDARDS_GOAL,
                ),
                Goal(appraisal=self, goal_type=Goal.GoalType.PERSONAL, order=2),
                Goal(appraisal=self, goal_type=Goal.GoalType.LEADERSHIP, order=3),
            ]
        )

    def __str__(self):
        return f"{self.teacher.email} — {self.academic_year}"


class Goal(models.Model):
    """A single goal within an appraisal.

    Carries both its setup (steps, success criteria) and its end-of-cycle review
    comments, so the same record represents the goal when set ("This Year") and
    when reviewed a year later ("Last Year").
    """

    class GoalType(models.TextChoices):
        STANDARDS = "STANDARDS", "Standards & policies"
        PERSONAL = "PERSONAL", "Personal goal"
        LEADERSHIP = "LEADERSHIP", "Leadership / UPR"

    appraisal = models.ForeignKey(
        Appraisal,
        on_delete=models.CASCADE,
        related_name="goals",
    )
    goal_type = models.CharField(max_length=20, choices=GoalType.choices)
    order = models.PositiveSmallIntegerField()

    title = models.TextField(blank=True, default="")
    steps_to_success = models.TextField(blank=True, default="")
    success_criteria = models.TextField(blank=True, default="")

    teacher_review_comment = models.TextField(blank=True, default="")
    coach_review_comment = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["appraisal", "order"],
                name="unique_goal_order_per_appraisal",
            ),
        ]

    def __str__(self):
        return f"{self.appraisal} — Goal {self.order} ({self.get_goal_type_display()})"


class SelfReview(models.Model):
    """A teacher's or support staff member's self-review for an appraisal.

    One per appraisal. The `kind` selects which fixed Copleston descriptor set is
    seeded into `SelfReviewItem` rows. Editing is governed by the parent
    `Appraisal.is_locked`; completion is tracked by
    `Appraisal.self_review_form_completed`.
    """

    class Kind(models.TextChoices):
        TEACHING = "TEACHING", "Teaching"
        SUPPORT = "SUPPORT", "Support"

    appraisal = models.OneToOneField(
        Appraisal,
        on_delete=models.CASCADE,
        related_name="self_review",
    )
    # Snapshot of the staff member's type at creation, so the review is stable if
    # their StaffMember.staff_type later changes.
    kind = models.CharField(max_length=20, choices=Kind.choices)

    # Support-only: pasted job-description sections.
    job_summary = models.TextField(blank=True, default="")
    level_description = models.TextField(blank=True, default="")

    # Teaching-only: Upper Pay Range declaration.
    upr_declaration_agreed = models.BooleanField(default=False)
    signed_name = models.CharField(max_length=200, blank=True, default="")
    signed_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def seed_items(self):
        """Create the descriptor group + bullet rows for this review.

        Uses the template matching `kind`. No-op if items already exist (so
        this also guards bullet creation, since bullets are only ever created
        alongside their parent item below). Called from the create view, not
        from save().
        """
        if self.items.exists():
            return
        template = TEACHING_ITEMS if self.kind == self.Kind.TEACHING else SUPPORT_ITEMS
        items = SelfReviewItem.objects.bulk_create(
            [
                SelfReviewItem(self_review=self, order=index + 1, code=code, heading=heading)
                for index, (code, heading, bullets) in enumerate(template)
            ]
        )
        SelfReviewBullet.objects.bulk_create(
            [
                SelfReviewBullet(self_review_item=item, order=bullet_index + 1, text=text)
                for item, (code, heading, bullets) in zip(items, template)
                for bullet_index, text in enumerate(bullets)
            ]
        )

    def __str__(self):
        return f"{self.appraisal} — Self-review ({self.get_kind_display()})"


class SelfReviewItem(models.Model):
    """A descriptor group within a self-review: a heading, its scorable
    bullets (see `SelfReviewBullet`), and one shared Evidence field.
    """

    self_review = models.ForeignKey(
        SelfReview,
        on_delete=models.CASCADE,
        related_name="items",
    )
    order = models.PositiveSmallIntegerField()
    code = models.CharField(max_length=20)
    heading = models.CharField(max_length=255, blank=True, default="")

    evidence = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["self_review", "code"],
                name="unique_item_code_per_self_review",
            ),
        ]

    def __str__(self):
        return f"{self.self_review} — {self.code}"


class SelfReviewBullet(models.Model):
    """A single scorable descriptor statement within a `SelfReviewItem` group.

    `text` is a snapshot of the fixed template wording, so rendering is
    self-contained and survives later edits to the template. `score` is
    Not Answered (null) or 1-3.
    """

    self_review_item = models.ForeignKey(
        SelfReviewItem,
        on_delete=models.CASCADE,
        related_name="bullets",
    )
    order = models.PositiveSmallIntegerField()
    text = models.TextField()

    score = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        choices=[(1, "1"), (2, "2"), (3, "3")],
    )

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["self_review_item", "order"],
                name="unique_bullet_order_per_item",
            ),
        ]

    def __str__(self):
        return f"{self.self_review_item} — bullet {self.order}"


class LeaderReview(models.Model):
    """A senior leader's (headteacher's) self-review against the Headteachers'
    Standards, run as the leader's variant of the self-review section.

    One per appraisal, seeded from `HEADTEACHER_STANDARDS`. Unlike `SelfReview`,
    scoring is per-standard rather than per-bullet, and the review carries its
    own free-form goals (`LeaderGoal`). Editing is governed by the parent
    `Appraisal.is_locked`, exactly like `SelfReview`.
    """

    appraisal = models.OneToOneField(
        Appraisal,
        on_delete=models.CASCADE,
        related_name="leader_review",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def seed_standards(self):
        """Create the 10 standard rows for this review from the template.

        No-op if standards already exist. Called from the create view, not from
        save(). Goals start empty (added/removed in the UI).
        """
        if self.standards.exists():
            return
        LeaderStandard.objects.bulk_create(
            [
                LeaderStandard(
                    leader_review=self,
                    order=index + 1,
                    number=number,
                    title=title,
                    descriptors="\n".join(descriptors),
                )
                for index, (number, title, descriptors) in enumerate(
                    HEADTEACHER_STANDARDS
                )
            ]
        )

    def __str__(self):
        return f"{self.appraisal} — Leadership self-review"


class LeaderStandard(models.Model):
    """One of the 10 Headteachers' Standards within a `LeaderReview`.

    `descriptors` is a newline-joined snapshot of the read-only prompt
    statements (not individually scored). The whole standard carries a single
    `score` (Not Answered / 1-3), a "Not in Job Role" toggle and free-text
    `examples`. When `not_applicable` is set the standard is excluded from
    scoring, so `score` is forced to null on save.
    """

    leader_review = models.ForeignKey(
        LeaderReview,
        on_delete=models.CASCADE,
        related_name="standards",
    )
    order = models.PositiveSmallIntegerField()
    number = models.PositiveSmallIntegerField()
    title = models.CharField(max_length=255)
    descriptors = models.TextField(blank=True, default="")

    score = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        choices=[(1, "1"), (2, "2"), (3, "3")],
    )
    not_applicable = models.BooleanField(default=False)
    examples = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["leader_review", "number"],
                name="unique_standard_number_per_leader_review",
            ),
        ]

    @property
    def descriptor_list(self):
        """The read-only prompt statements as a list (for template rendering)."""
        return [line for line in self.descriptors.split("\n") if line]

    def save(self, *args, **kwargs):
        # A standard marked "Not in Job Role" is never scored.
        if self.not_applicable:
            self.score = None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.leader_review} — Standard {self.number}"


class LeaderGoal(models.Model):
    """A free-form goal arising from a leader's self-review.

    Added/removed by the reviewee in the UI (no fixed set, unlike `Goal`).
    """

    leader_review = models.ForeignKey(
        LeaderReview,
        on_delete=models.CASCADE,
        related_name="goals",
    )
    order = models.PositiveSmallIntegerField(default=0)
    goal = models.TextField(blank=True, default="")
    evidence_and_discussion = models.TextField(blank=True, default="")
    achieved = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.leader_review} — goal {self.pk}"

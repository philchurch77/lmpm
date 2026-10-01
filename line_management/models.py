from django.db import models

from core.models import StaffMember

# Helper text shown on the single rotation field. Each line meeting covers one
# rotation in turn; the manager records the relevant update here.
ROTATION_GUIDANCE = (
    "Rotation 1 — Quality Assurance (tasks / findings / actions) · "
    "Rotation 2 — Staff matters · "
    "Rotation 3 — Update on one of: development-plan priorities, "
    "Professional Growth targets, or data targets (e.g. attendance or results)."
)


class LineMeeting(models.Model):
    """A single line-management meeting record for one staff member.

    Unlike an appraisal (one per teacher per year), a staff member has many
    dated line meetings over time. The managed person views their own records
    read-only; the staff member's **current** line manager edits them.

    Authorization (see ``permissions.meeting_role``) is a **live** lookup
    against ``staff.line_manager_email``, not a snapshot. This is a deliberate
    governance decision: when a person changes line manager, the successor
    inherits read+edit access to the whole history and the previous manager
    loses access. ``created_by_email`` records who actually wrote each meeting
    (display/provenance only — never used for access decisions), so an inherited
    note is always attributed to its original author.
    """

    staff = models.ForeignKey(
        StaffMember,
        on_delete=models.PROTECT,
        related_name="line_meetings",
    )
    # Who created this record. Provenance/display only; stamped server-side from
    # the acting user and never used for authorization.
    created_by_email = models.EmailField(blank=True, default="")

    meeting_date = models.DateField()

    # The five sections of the line-meeting form.
    actions_from_last_meeting = models.TextField(blank=True, default="")
    upcoming = models.TextField(blank=True, default="")
    rotation_update = models.TextField(blank=True, default="")
    main_matters = models.TextField(blank=True, default="")
    actions_from_meeting = models.TextField(blank=True, default="")

    # The note sections that carry a meeting's content. A record with all of
    # these blank holds no notes (only a date) — see ``is_empty``.
    NOTE_FIELDS = (
        "actions_from_last_meeting",
        "upcoming",
        "rotation_update",
        "main_matters",
        "actions_from_meeting",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-meeting_date", "-created_at"]
        indexes = [
            models.Index(fields=["staff", "-meeting_date"]),
        ]

    def save(self, *args, **kwargs):
        self.created_by_email = self.created_by_email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def is_empty(self) -> bool:
        """True when the record holds only a date: no note section has content and
        no action was agreed at or reviewed in it.

        Records are no longer created until the manager saves, so this should be
        rare; ``purge_empty_line_meetings`` uses the same rule to clean up legacy
        blanks. ``NOTE_FIELDS`` is deliberately not extended with the actions —
        the importer's ``source_row_hash`` is computed from it.
        """
        if any((getattr(self, f) or "").strip() for f in self.NOTE_FIELDS):
            return False
        if self.pk is None:
            return True
        return not (self.agreed_actions.exists() or self.reviewed_actions.exists())

    def __str__(self):
        return f"{self.staff.email} — {self.meeting_date}"


class MeetingAction(models.Model):
    """One action agreed at a line meeting, reviewed (RAG-rated) at the next.

    Like ``appraisals.Goal`` spanning two years, one row spans two meetings: it is
    created at ``agreed_at`` and pinned to ``reviewed_in`` when the following
    meeting is created (``services.start_meeting``). The RAG rating and review
    comment belong to the review, so they may only be set once ``reviewed_in``
    exists (enforced in the database).

    Both FKs are PROTECT: deleting meeting N must never silently destroy the
    ratings and comments written at meeting N+1.
    """

    class Rag(models.TextChoices):
        RED = "RED", "Red"
        AMBER = "AMBER", "Amber"
        GREEN = "GREEN", "Green"

    agreed_at = models.ForeignKey(
        LineMeeting,
        on_delete=models.PROTECT,
        related_name="agreed_actions",
    )
    reviewed_in = models.ForeignKey(
        LineMeeting,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_actions",
    )
    # What / by whom / by when.
    description = models.TextField()
    # Blank = not yet rated.
    rag = models.CharField(max_length=5, choices=Rag.choices, blank=True, default="")
    review_comment = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(reviewed_in=models.F("agreed_at")),
                name="meetingaction_not_reviewed_where_agreed",
            ),
            models.CheckConstraint(
                condition=models.Q(rag__in=["", "RED", "AMBER", "GREEN"]),
                name="meetingaction_rag_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(reviewed_in__isnull=False)
                | models.Q(rag="", review_comment=""),
                name="meetingaction_rating_needs_review_meeting",
            ),
        ]

    @property
    def is_pinned(self) -> bool:
        """Carried into a later meeting: its wording is settled and it can't be deleted."""
        return self.reviewed_in_id is not None

    def __str__(self):
        # Never the description: this appears in admin and logs.
        return f"Action {self.pk} — agreed {self.agreed_at}"

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
        """True when no note section has content (the record holds only a date).

        Records are no longer created until the manager saves, so this should be
        rare; ``purge_empty_line_meetings`` uses it to clean up legacy blanks.
        """
        return not any((getattr(self, f) or "").strip() for f in self.NOTE_FIELDS)

    def __str__(self):
        return f"{self.staff.email} — {self.meeting_date}"

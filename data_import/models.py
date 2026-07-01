"""Models backing the bulk CSV import subsystem.

An import is staged in two steps: upload (parse the CSV, store one
``ImportRow`` per source row with its planned outcome) and confirm (apply each
row's create/update against the real models). This lets a superuser preview
exactly what an upload will do before anything is written, and keeps a
permanent audit trail — batches and rows are never deleted.

Re-running the same (or a corrected) file must not duplicate data. Four of the
five import types upsert on a real model uniqueness constraint (e.g.
``unique_appraisal_per_teacher_year``), which makes that automatic. LineMeeting
has none — multiple genuine meetings can share a staff+date — so its dedupe
relies on ``ImportRow.source_row_hash``, checked across *every* batch of that
import type, not just the current one (see ``data_import.services``).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ImportType(models.TextChoices):
    STAFF = "STAFF", "Staff"
    APPRAISAL_SUMMARY = "APPRAISAL_SUMMARY", "Appraisal summaries"
    GOALS = "GOALS", "Goals"
    SELF_REVIEW = "SELF_REVIEW", "Self-review"
    LINE_MEETINGS = "LINE_MEETINGS", "Line meetings"


class ImportedModel(models.TextChoices):
    """The only models an ImportRow is ever allowed to point at.

    A closed choice set rather than a free-text app_label/model pair, so a
    future model rename is caught as a code change here, not as a silently
    broken dedupe/audit lookup.
    """

    STAFF_MEMBER = "StaffMember", "StaffMember"
    APPRAISAL = "Appraisal", "Appraisal"
    GOAL = "Goal", "Goal"
    SELF_REVIEW_BULLET = "SelfReviewBullet", "SelfReviewBullet"
    LINE_MEETING = "LineMeeting", "LineMeeting"


class ImportBatch(models.Model):
    """One CSV upload: a set of parsed rows awaiting, or having received, a decision."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        CONFIRMED = "CONFIRMED", "Confirmed"
        DISCARDED = "DISCARDED", "Discarded"

    import_type = models.CharField(max_length=30, choices=ImportType.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    original_filename = models.CharField(max_length=255, blank=True, default="")

    # Aggregate outcome counts, computed from this batch's rows right after
    # parsing so the hub/preview pages render summary numbers without
    # re-scanning every row.
    create_count = models.PositiveIntegerField(default=0)
    update_count = models.PositiveIntegerField(default=0)
    skip_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.get_import_type_display()} batch #{self.pk} ({self.get_status_display()})"


class ImportRow(models.Model):
    """One parsed CSV row: its planned outcome, and (after confirm) what it did.

    ``import_type`` is denormalised from ``batch.import_type`` so the
    cross-batch dedupe lookup (``import_type`` + ``source_row_hash``) is a
    single indexed query, not a join through every batch of that type.
    """

    class Outcome(models.TextChoices):
        CREATE = "CREATE", "Create"
        UPDATE = "UPDATE", "Update"
        SKIP = "SKIP", "Skip"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    import_type = models.CharField(max_length=30, choices=ImportType.choices)
    row_number = models.PositiveIntegerField()
    raw_json = models.JSONField()

    outcome = models.CharField(max_length=10, choices=Outcome.choices)
    error_message = models.CharField(max_length=500, blank=True, default="")

    # SHA-256 of the row's normalised natural-key fields (see
    # data_import.services.row_hash). Used for cross-batch dedupe where the
    # target model has no real uniqueness constraint to upsert on.
    source_row_hash = models.CharField(max_length=64)

    created_object_model = models.CharField(
        max_length=30, choices=ImportedModel.choices, blank=True, default=""
    )
    created_object_pk = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["row_number"]
        indexes = [
            models.Index(fields=["batch", "row_number"]),
            models.Index(fields=["import_type", "source_row_hash"]),
        ]

    def __str__(self):
        return f"{self.batch} — row {self.row_number} ({self.outcome})"

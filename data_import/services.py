"""Validation and apply logic for each bulk-import CSV type.

Each import type has a ``validate_*`` function and an ``apply_*`` function.
``validate_*`` is pure (read-only DB lookups, no writes) and is called twice:
once at upload time, to populate ``ImportRow.outcome``/``error_message`` for
the preview, and again at confirm time, immediately before ``apply_*`` runs,
to catch drift between preview and confirm (e.g. a referenced StaffMember
deleted in the meantime). A row that validates at upload but not at confirm is
recorded as a fresh SKIP rather than raising.

``apply_*`` only ever runs inside a ``transaction.atomic()`` block scoped to
one logical unit of work (one row, or for self-review, one seed-the-review
step shared by a group of bullet rows). The caller catches any exception
*outside* that block — never inside it, which would leave the connection in a
poisoned transaction state until the block exits.

Four of the five types upsert on a real model uniqueness constraint, which
makes re-running a batch naturally idempotent. ``LineMeeting`` has none
(multiple genuine meetings can share a staff+date), so its dedupe instead
checks ``source_row_hash`` against every previously-applied row of that import
type, across all batches — see ``validate_line_meeting_row``.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db import transaction
from django.utils import timezone

from appraisals.models import (
    AcademicYear,
    Appraisal,
    Goal,
    SelfReview,
    SelfReviewBullet,
    SelfReviewItem,
)
from appraisals.self_review_templates import SUPPORT_ITEMS, TEACHING_ITEMS
from core.models import School, StaffMember
from line_management.models import LineMeeting

from .models import ImportBatch, ImportedModel, ImportRow, ImportType

TRUE_STRINGS = {"1", "true", "yes", "y"}


def parse_bool(value: str) -> bool:
    return value.strip().lower() in TRUE_STRINGS


def normalise_email(value: str) -> str:
    return (value or "").strip().lower()


def _set_if_present(data: dict, defaults: dict, field_name: str, transform=str.strip) -> None:
    """Write ``defaults[field_name]`` only when the CSV cell is non-blank.

    Shared by every ``apply_*`` function so "a blank cell must not overwrite
    an existing non-blank value" (see docs/import_templates.md) is enforced
    identically everywhere, rather than re-spelled per import type.
    """
    raw = data.get(field_name, "")
    if raw.strip():
        defaults[field_name] = transform(raw)


def _set_bool_if_present(data: dict, defaults: dict, field_name: str) -> None:
    _set_if_present(data, defaults, field_name, transform=parse_bool)


@dataclass
class ValidationResult:
    ok: bool
    outcome: str
    errors: list[str] = field(default_factory=list)
    resolved: dict[str, Any] = field(default_factory=dict)


def row_hash(import_type: str, fields: dict) -> str:
    """Deterministic hash of a row's fields, used for the audit trail and
    (for LineMeeting only) functional cross-batch dedupe."""
    payload = json.dumps({"type": import_type, **fields}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_school(name: str):
    name = (name or "").strip()
    if not name:
        return None, None
    school = School.objects.filter(name__iexact=name).first()
    if school is None:
        return None, f"No school named '{name}'."
    return school, None


def resolve_staff(email: str):
    email = normalise_email(email)
    if not email:
        return None, "Missing email."
    staff = StaffMember.objects.filter(email__iexact=email).first()
    if staff is None:
        return None, f"No staff member with email '{email}'."
    return staff, None


def resolve_academic_year(start_year_raw: str):
    start_year_raw = (start_year_raw or "").strip()
    if not start_year_raw:
        return None, "Missing academic_year."
    try:
        start_year = int(start_year_raw)
    except ValueError:
        return None, f"academic_year '{start_year_raw}' is not a number."
    year = AcademicYear.objects.filter(start_year=start_year).first()
    if year is None:
        return None, f"No academic year starting {start_year}."
    return year, None


def resolve_appraisal(staff: StaffMember, year: AcademicYear):
    return Appraisal.objects.filter(teacher=staff, academic_year=year).first()


# --- Staff -------------------------------------------------------------


def validate_staff_row(data: dict, uploaded_by_email: str = "") -> ValidationResult:
    email = normalise_email(data.get("email", ""))
    if not email:
        return ValidationResult(False, ImportRow.Outcome.SKIP, ["Missing email."])

    errors = []
    school, err = resolve_school(data.get("school", ""))
    if err:
        errors.append(err)

    staff_type = data.get("staff_type", "").strip().upper()
    if staff_type and staff_type not in StaffMember.StaffType.values:
        errors.append(f"staff_type '{staff_type}' is not TEACHING or SUPPORT.")

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    exists = StaffMember.objects.filter(email__iexact=email).exists()
    outcome = ImportRow.Outcome.UPDATE if exists else ImportRow.Outcome.CREATE
    return ValidationResult(
        True, outcome, [], {"email": email, "school": school, "staff_type": staff_type}
    )


def apply_staff_row(data: dict, resolved: dict):
    defaults = {}
    _set_if_present(data, defaults, "line_manager_email", transform=normalise_email)
    _set_if_present(data, defaults, "performance_manager_email", transform=normalise_email)
    _set_if_present(data, defaults, "department")
    _set_if_present(data, defaults, "job_title")
    if resolved.get("staff_type"):
        defaults["staff_type"] = resolved["staff_type"]
    if resolved.get("school") is not None:
        defaults["school"] = resolved["school"]

    staff, _ = StaffMember.objects.update_or_create(
        email=resolved["email"], defaults=defaults
    )
    return ImportedModel.STAFF_MEMBER, staff.pk


# --- Appraisal summaries -------------------------------------------------


def validate_appraisal_summary_row(
    data: dict, uploaded_by_email: str = ""
) -> ValidationResult:
    errors = []
    staff, err = resolve_staff(data.get("teacher_email", ""))
    if err:
        errors.append(err)
    year, err = resolve_academic_year(data.get("academic_year", ""))
    if err:
        errors.append(err)

    status = data.get("status", "").strip().upper()
    if status and status not in Appraisal.Status.values:
        errors.append(f"status '{status}' is not DRAFT, SHARED, or SIGNED_OFF.")

    pay_award = data.get("coach_supports_pay_award", "").strip().upper()
    if pay_award and pay_award not in Appraisal.PayAward.values:
        errors.append(
            f"coach_supports_pay_award '{pay_award}' is not YES, NO, or NOT_APPLICABLE."
        )

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    exists = Appraisal.objects.filter(teacher=staff, academic_year=year).exists()
    outcome = ImportRow.Outcome.UPDATE if exists else ImportRow.Outcome.CREATE
    return ValidationResult(
        True,
        outcome,
        [],
        {"staff": staff, "year": year, "status": status, "pay_award": pay_award},
    )


def apply_appraisal_summary_row(data: dict, resolved: dict):
    staff = resolved["staff"]
    year = resolved["year"]

    coach_email = data.get("coach_email", "").strip()
    defaults = {
        "coach_email": normalise_email(coach_email)
        if coach_email
        else staff.performance_manager_email
    }
    if resolved.get("status"):
        defaults["status"] = resolved["status"]
    if resolved.get("pay_award"):
        defaults["coach_supports_pay_award"] = resolved["pay_award"]
    for bool_field in (
        "on_upper_pay_range",
        "self_review_form_completed",
        "engaged_with_professional_growth",
        "job_description_review_needed",
    ):
        _set_bool_if_present(data, defaults, bool_field)
    for text_field in (
        "cpd_requirements",
        "summary_teacher_comment",
        "summary_coach_comment",
    ):
        _set_if_present(data, defaults, text_field)

    appraisal, created = Appraisal.objects.update_or_create(
        teacher=staff, academic_year=year, defaults=defaults
    )
    if created:
        appraisal.seed_goals()
    return ImportedModel.APPRAISAL, appraisal.pk


# --- Goals ---------------------------------------------------------------

GOAL_TYPE_ORDER = {
    Goal.GoalType.STANDARDS: 1,
    Goal.GoalType.PERSONAL: 2,
    Goal.GoalType.LEADERSHIP: 3,
}


def validate_goals_row(data: dict, uploaded_by_email: str = "") -> ValidationResult:
    errors = []
    staff, err = resolve_staff(data.get("teacher_email", ""))
    if err:
        errors.append(err)
    year, err = resolve_academic_year(data.get("academic_year", ""))
    if err:
        errors.append(err)

    goal_type = data.get("goal_type", "").strip().upper()
    if goal_type not in Goal.GoalType.values:
        errors.append(
            f"goal_type '{goal_type}' is not STANDARDS, PERSONAL, or LEADERSHIP."
        )

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    appraisal = resolve_appraisal(staff, year)
    if appraisal is None:
        return ValidationResult(
            False,
            ImportRow.Outcome.SKIP,
            [
                f"No appraisal exists yet for {staff.email} / {year}. "
                "Import appraisal_summaries.csv first."
            ],
        )

    order = GOAL_TYPE_ORDER[goal_type]
    exists = Goal.objects.filter(appraisal=appraisal, order=order).exists()
    outcome = ImportRow.Outcome.UPDATE if exists else ImportRow.Outcome.CREATE
    return ValidationResult(
        True, outcome, [], {"appraisal": appraisal, "goal_type": goal_type, "order": order}
    )


def apply_goals_row(data: dict, resolved: dict):
    defaults = {"goal_type": resolved["goal_type"]}
    for text_field in (
        "title",
        "steps_to_success",
        "success_criteria",
        "teacher_review_comment",
        "coach_review_comment",
    ):
        _set_if_present(data, defaults, text_field)

    goal, _ = Goal.objects.update_or_create(
        appraisal=resolved["appraisal"], order=resolved["order"], defaults=defaults
    )
    return ImportedModel.GOAL, goal.pk


# --- Self-review -----------------------------------------------------------


def _template_for_staff(staff: StaffMember):
    return SUPPORT_ITEMS if staff.staff_type == StaffMember.StaffType.SUPPORT else TEACHING_ITEMS


def validate_self_review_row(data: dict, uploaded_by_email: str = "") -> ValidationResult:
    errors = []
    staff, err = resolve_staff(data.get("teacher_email", ""))
    if err:
        errors.append(err)
    year, err = resolve_academic_year(data.get("academic_year", ""))
    if err:
        errors.append(err)

    item_code = data.get("item_code", "").strip()
    if not item_code:
        errors.append("Missing item_code.")

    bullet_order = None
    bullet_order_raw = data.get("bullet_order", "").strip()
    if not bullet_order_raw:
        errors.append("Missing bullet_order.")
    else:
        try:
            bullet_order = int(bullet_order_raw)
        except ValueError:
            errors.append(f"bullet_order '{bullet_order_raw}' is not a number.")

    score = None
    score_raw = data.get("score", "").strip()
    if score_raw:
        if score_raw not in {"1", "2", "3"}:
            errors.append(f"score '{score_raw}' must be 1, 2, 3, or blank.")
        else:
            score = int(score_raw)

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    appraisal = resolve_appraisal(staff, year)
    if appraisal is None:
        return ValidationResult(
            False,
            ImportRow.Outcome.SKIP,
            [
                f"No appraisal exists yet for {staff.email} / {year}. "
                "Import appraisal_summaries.csv first."
            ],
        )

    template = _template_for_staff(staff)
    template_item = next((entry for entry in template if entry[0] == item_code), None)
    if template_item is None:
        errors.append(
            f"item_code '{item_code}' is not a current descriptor code for "
            f"{staff.email}'s staff type."
        )
    elif not (1 <= bullet_order <= len(template_item[2])):
        errors.append(
            f"bullet_order {bullet_order} is out of range for item '{item_code}' "
            f"(it has {len(template_item[2])} bullets)."
        )

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    return ValidationResult(
        True,
        ImportRow.Outcome.UPDATE,
        [],
        {
            "staff": staff,
            "year": year,
            "appraisal": appraisal,
            "item_code": item_code,
            "bullet_order": bullet_order,
            "score": score,
            "evidence": data.get("evidence", "").strip(),
        },
    )


def ensure_self_review_seeded(appraisal: Appraisal, staff: StaffMember) -> SelfReview:
    """Get-or-create + seed the appraisal's self-review.

    Its own atomic unit, run once per (teacher, year) group before any bullet
    row in that group is applied — ``seed_items()`` bulk-creates the whole
    item+bullet tree in one shot, so it must not be interleaved with per-bullet
    updates (a process death mid-file must never leave a half-seeded review).
    """
    with transaction.atomic():
        kind = (
            SelfReview.Kind.SUPPORT
            if staff.staff_type == StaffMember.StaffType.SUPPORT
            else SelfReview.Kind.TEACHING
        )
        self_review, _ = SelfReview.objects.get_or_create(
            appraisal=appraisal, defaults={"kind": kind}
        )
        self_review.seed_items()
    return self_review


def apply_self_review_row(self_review: SelfReview, resolved: dict):
    item = SelfReviewItem.objects.get(self_review=self_review, code=resolved["item_code"])
    bullet = SelfReviewBullet.objects.get(
        self_review_item=item, order=resolved["bullet_order"]
    )

    # Evidence is shared per item group on the form, so it's only read from
    # the bullet_order=1 row for a given item_code (see docs/import_templates.md).
    if resolved["bullet_order"] == 1 and resolved["evidence"]:
        item.evidence = resolved["evidence"]
        item.save(update_fields=["evidence"])

    if resolved["score"] is not None:
        bullet.score = resolved["score"]
        bullet.save(update_fields=["score"])

    return ImportedModel.SELF_REVIEW_BULLET, bullet.pk


# --- Line meetings -----------------------------------------------------------


def validate_line_meeting_row(data: dict, uploaded_by_email: str = "") -> ValidationResult:
    errors = []
    staff, err = resolve_staff(data.get("staff_email", ""))
    if err:
        errors.append(err)

    meeting_date = None
    date_raw = data.get("meeting_date", "").strip()
    if not date_raw:
        errors.append("Missing meeting_date.")
    else:
        try:
            meeting_date = date.fromisoformat(date_raw)
        except ValueError:
            errors.append(f"meeting_date '{date_raw}' is not in YYYY-MM-DD format.")

    notes = {f: data.get(f, "").strip() for f in LineMeeting.NOTE_FIELDS}
    if not any(notes.values()):
        errors.append("All five note fields are blank — meeting has no content.")

    if errors:
        return ValidationResult(False, ImportRow.Outcome.SKIP, errors)

    created_by = normalise_email(data.get("created_by_email", "")) or normalise_email(
        uploaded_by_email
    )
    hash_fields = {"staff_email": staff.email, "meeting_date": str(meeting_date), **notes}
    hash_value = row_hash(ImportType.LINE_MEETINGS, hash_fields)

    existing_row = (
        ImportRow.objects.filter(
            import_type=ImportType.LINE_MEETINGS,
            source_row_hash=hash_value,
            created_object_pk__isnull=False,
        )
        .order_by("-batch__uploaded_at")
        .first()
    )
    existing_pk = existing_row.created_object_pk if existing_row else None
    outcome = ImportRow.Outcome.UPDATE if existing_pk else ImportRow.Outcome.CREATE

    return ValidationResult(
        True,
        outcome,
        [],
        {
            "staff": staff,
            "meeting_date": meeting_date,
            "created_by": created_by,
            "notes": notes,
            "existing_pk": existing_pk,
            "hash": hash_value,
        },
    )


def apply_line_meeting_row(data: dict, resolved: dict):
    defaults = dict(resolved["notes"])
    if resolved["created_by"]:
        defaults["created_by_email"] = resolved["created_by"]

    if resolved["existing_pk"] and LineMeeting.objects.filter(pk=resolved["existing_pk"]).exists():
        LineMeeting.objects.filter(pk=resolved["existing_pk"]).update(**defaults)
        return ImportedModel.LINE_MEETING, resolved["existing_pk"]

    meeting = LineMeeting.objects.create(
        staff=resolved["staff"], meeting_date=resolved["meeting_date"], **defaults
    )
    return ImportedModel.LINE_MEETING, meeting.pk


# --- Dispatch + batch orchestration -----------------------------------------

# SELF_REVIEW is handled separately (it needs per-teacher/year grouping so
# seed_items() runs once per group, not once per bullet row) — see
# build_rows_for_batch and confirm_batch below.
IMPORTERS = {
    ImportType.STAFF: (validate_staff_row, apply_staff_row),
    ImportType.APPRAISAL_SUMMARY: (
        validate_appraisal_summary_row,
        apply_appraisal_summary_row,
    ),
    ImportType.GOALS: (validate_goals_row, apply_goals_row),
    ImportType.LINE_MEETINGS: (validate_line_meeting_row, apply_line_meeting_row),
}


def _default_row_hash(import_type: str, data: dict) -> str:
    return row_hash(import_type, data)


def build_rows_for_batch(batch: ImportBatch, parsed_rows: list[tuple[int, dict]]) -> None:
    """Validate every parsed row and bulk-create the matching ImportRow set.

    Updates and saves the batch's aggregate outcome counts. Uses bulk_create
    (not a per-row .save() loop) so a large file doesn't turn into thousands
    of individual INSERTs before the preview can even render.
    """
    import_type = batch.import_type
    validate_fn = (
        validate_self_review_row
        if import_type == ImportType.SELF_REVIEW
        else IMPORTERS[import_type][0]
    )
    uploaded_by_email = getattr(batch.uploaded_by, "email", "") or ""

    to_create = []
    counts: Counter = Counter()
    for row_number, data in parsed_rows:
        result = validate_fn(data, uploaded_by_email)
        counts[result.outcome] += 1
        if import_type == ImportType.LINE_MEETINGS and result.ok:
            hash_value = result.resolved["hash"]
        else:
            hash_value = _default_row_hash(import_type, data)
        to_create.append(
            ImportRow(
                batch=batch,
                import_type=import_type,
                row_number=row_number,
                raw_json=data,
                outcome=result.outcome,
                error_message="; ".join(result.errors),
                source_row_hash=hash_value,
            )
        )
    ImportRow.objects.bulk_create(to_create)

    batch.create_count = counts.get(ImportRow.Outcome.CREATE, 0)
    batch.update_count = counts.get(ImportRow.Outcome.UPDATE, 0)
    batch.skip_count = counts.get(ImportRow.Outcome.SKIP, 0)
    batch.save(update_fields=["create_count", "update_count", "skip_count"])


def _skip_row(row: ImportRow, message: str) -> None:
    row.outcome = ImportRow.Outcome.SKIP
    row.error_message = message
    row.save(update_fields=["outcome", "error_message"])


def _mark_applied(row: ImportRow, outcome: str, model_name: str, pk: int) -> None:
    row.outcome = outcome
    row.created_object_model = model_name
    row.created_object_pk = pk
    row.save(update_fields=["outcome", "created_object_model", "created_object_pk"])


def _confirm_one_row(row: ImportRow, validate_fn, apply_fn, uploaded_by_email: str = "") -> None:
    """Re-validate, then apply, one row's planned outcome.

    Shared by every import type's confirm loop, including self-review's (via
    a closure binding ``apply_self_review_row`` to its group's ``SelfReview``
    — see ``_confirm_self_review_batch``) so the validate/apply/record-outcome
    mechanics live in exactly one place.
    """
    result = validate_fn(row.raw_json, uploaded_by_email)
    if not result.ok:
        _skip_row(row, "; ".join(result.errors) or "No longer valid at confirm time.")
        return

    try:
        with transaction.atomic():
            model_name, pk = apply_fn(row.raw_json, result.resolved)
    except Exception as exc:
        _skip_row(row, f"Failed to apply: {exc}")
        return

    _mark_applied(row, result.outcome, model_name, pk)


def _confirm_self_review_batch(rows: list[ImportRow]) -> None:
    groups: dict[tuple[str, str], list[ImportRow]] = {}
    for row in rows:
        key = (
            row.raw_json.get("teacher_email", "").strip().lower(),
            row.raw_json.get("academic_year", "").strip(),
        )
        groups.setdefault(key, []).append(row)

    for (teacher_email, academic_year_raw), group_rows in groups.items():
        staff, staff_err = resolve_staff(teacher_email)
        year, year_err = resolve_academic_year(academic_year_raw)
        if staff is None or year is None:
            for row in group_rows:
                _skip_row(row, staff_err or year_err)
            continue

        appraisal = resolve_appraisal(staff, year)
        if appraisal is None:
            for row in group_rows:
                _skip_row(row, "Appraisal no longer exists at confirm time.")
            continue

        try:
            self_review = ensure_self_review_seeded(appraisal, staff)
        except Exception as exc:
            for row in group_rows:
                _skip_row(row, f"Failed to prepare self-review: {exc}")
            continue

        def apply_bullet(data, resolved, self_review=self_review):
            return apply_self_review_row(self_review, resolved)

        for row in group_rows:
            _confirm_one_row(row, validate_self_review_row, apply_bullet)


def confirm_batch(batch: ImportBatch) -> None:
    """Apply every non-SKIP row in the batch, then mark it CONFIRMED."""
    rows = list(batch.rows.exclude(outcome=ImportRow.Outcome.SKIP).order_by("row_number"))

    if batch.import_type == ImportType.SELF_REVIEW:
        _confirm_self_review_batch(rows)
    else:
        validate_fn, apply_fn = IMPORTERS[batch.import_type]
        uploaded_by_email = getattr(batch.uploaded_by, "email", "") or ""
        for row in rows:
            _confirm_one_row(row, validate_fn, apply_fn, uploaded_by_email)

    batch.status = ImportBatch.Status.CONFIRMED
    batch.confirmed_at = timezone.now()
    batch.save(update_fields=["status", "confirmed_at"])

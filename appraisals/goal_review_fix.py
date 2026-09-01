"""Core logic for the one-off correction of misplaced goal review comments.

Shared by two front ends — the ``move_prior_year_goal_reviews`` management
command and the "Move misplaced goal reviews" Django admin action — so the
decision rules live in exactly one place and are tested once.

The fault
---------
The trust's first system was a PowerApps/SharePoint list holding one row per
teacher per year. That row carried **two** separate blocks of review columns:

* ``Review of Goal N`` — the performance manager's review of the *previous*
  year's goals, written at the start of the year.
* ``Goal N Review`` — the interim review of *that* year's goals, written from
  December onwards.

The bulk import mapped the **first** block onto the goals of the year the row
belonged to, so a review written in September 2025 about the 2024/25 goals
ended up stored on the 2025/26 ``Goal`` rows.

Nothing is structurally malformed, which is why this is invisible in the admin:
a ``Goal`` with review text looks entirely normal. It is wrong only in meaning —
the comment does not describe the goal it is attached to.

The correction moves those comments back one year, onto ``Goal`` rows of the
year they actually review. ``Appraisal.previous()`` looks exactly one year back,
so the reviews then surface on the source year's "Last Year" tab, and the
following year's tab is left empty for the coach.

Shape
-----
Plan, then apply — the same split ``data_import`` uses. Every decision is made
once, in :func:`build_plan`, and both the preview and the write consume that
same plan, so a dry run cannot describe something different from what runs.

Safety
------
* **Live coach work is never overwritten.** Each goal's stored text is compared
  against the ``ImportRow.raw_json`` that wrote it. A mismatch — or no import
  row at all — means a human has been in there since, so the whole appraisal is
  flagged and left untouched. The check is re-run inside the transaction, under
  a row lock where the database supports one, because a coach may save between
  the plan and the write.

  Note what this proves: that the text still equals what the last recorded
  import wrote. It is not proof that no human ever edited it.
* **Rows outside the source year are never read or written.** Be aware, though,
  that the two fields being cleared are the same two the *following* year's
  "Last Year" tab writes into — which is exactly why the guard above is
  load-bearing rather than belt-and-braces.
* Idempotent: once moved, the source fields are blank, so a second run is a
  no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.db import connection, transaction

from appraisals.models import AcademicYear, Appraisal, Goal
from data_import.models import ImportedModel, ImportRow, ImportType

# The years this correction was written for. Overridable so the logic is
# testable and re-usable, but the defaults describe the actual fault.
DEFAULT_FROM_YEAR = 2025
DEFAULT_TO_YEAR = 2024

# The moved reviews describe goals the system never held — the app was built at
# the end of 2024/25. Say so on the goal rather than leaving a blank that looks
# like data went missing.
PLACEHOLDER_TITLE = "Goal not recorded — this year predates the system."

REVIEW_FIELDS = ("teacher_review_comment", "coach_review_comment")

# Why a goal cannot be moved automatically. The two flag reasons need opposite
# responses from a human, so they are never collapsed into one message.
NO_IMPORT_ROW = "created in the app, not imported — this is live work"
TEXT_DIFFERS = "text differs from the import — edited since"
TARGET_OCCUPIED = "target goal already holds a review"


@dataclass
class GoalMove:
    goal: Goal
    target_exists: bool


@dataclass
class AppraisalPlan:
    """One appraisal's decided outcome. Computed once, consumed by both modes."""

    appraisal: Appraisal
    moves: list[GoalMove] = field(default_factory=list)
    blocked: list[tuple[Goal, str]] = field(default_factory=list)
    flagged: list[tuple[Goal, str]] = field(default_factory=list)
    target_appraisal_exists: bool = False

    @property
    def is_flagged(self) -> bool:
        return bool(self.flagged)


class AdjacentYearError(ValueError):
    """Raised when the two years are not consecutive."""


def check_years(from_start: int, to_start: int) -> None:
    """Appraisal.previous() looks exactly one year back.

    A non-adjacent move would succeed, report success, and leave the reviews
    invisible to every view in the app.
    """
    if to_start != from_start - 1:
        raise AdjacentYearError(
            f"The target year must be the source year minus 1 "
            f"(got {to_start} and {from_start}). Appraisal.previous() only "
            "looks one year back, so a wider gap would hide the reviews."
        )


# --- planning -------------------------------------------------------------


def build_plan(source_year, to_start: int, teacher_email: str = "") -> list[AppraisalPlan]:
    """Decide every goal's outcome. Read-only — writes nothing."""
    appraisals = (
        Appraisal.objects.filter(academic_year=source_year)
        .select_related("teacher", "academic_year")
        .prefetch_related("goals")
        .order_by("teacher__email")
    )
    if teacher_email:
        appraisals = appraisals.filter(teacher__email__iexact=teacher_email.strip())

    target_year = AcademicYear.objects.filter(start_year=to_start).first()
    plans = []

    for appraisal in appraisals:
        goals = [g for g in appraisal.goals.all() if has_review(g)]
        if not goals:
            continue

        plan = AppraisalPlan(appraisal=appraisal)

        # One SharePoint row produced all three goals, so they are one unit of
        # doubt: if any was touched, none of them move.
        for goal in goals:
            reason = edit_reason(goal)
            if reason:
                plan.flagged.append((goal, reason))
        if plan.is_flagged:
            plans.append(plan)
            continue

        target = (
            Appraisal.objects.filter(
                teacher=appraisal.teacher, academic_year=target_year
            ).first()
            if target_year
            else None
        )
        plan.target_appraisal_exists = target is not None

        for goal in goals:
            existing = (
                Goal.objects.filter(appraisal=target, order=goal.order).first()
                if target
                else None
            )
            if existing is not None and has_review(existing):
                plan.blocked.append((goal, TARGET_OCCUPIED))
            else:
                plan.moves.append(GoalMove(goal=goal, target_exists=existing is not None))

        plans.append(plan)

    return plans


def restrict_to_approved(
    plans: list[AppraisalPlan], approved_goal_pks: set[int]
) -> tuple[list[AppraisalPlan], list[tuple[str, int]]]:
    """Narrow a freshly-built plan to the goals an operator actually approved.

    The plan is rebuilt on the confirm request rather than carried across, so
    that the safety guard is re-evaluated against current data. That is right
    for drift in the safe direction — a goal edited in the meantime drops out —
    but a goal can also *enter* the plan between preview and confirm (a goals
    import confirmed in the interim gives a previously-unimported goal an
    ``ImportRow``, flipping it from untouchable to movable).

    Without this, the operator could approve a named list of staff and apply a
    longer one. Anything new is returned separately to be reported, never
    applied.
    """
    newly_appeared: list[tuple[str, int]] = []
    restricted: list[AppraisalPlan] = []

    for plan in plans:
        keep = []
        for move in plan.moves:
            if move.goal.pk in approved_goal_pks:
                keep.append(move)
            else:
                newly_appeared.append((plan.appraisal.teacher.email, move.goal.order))
        if keep:
            restricted.append(
                AppraisalPlan(
                    appraisal=plan.appraisal,
                    moves=keep,
                    blocked=plan.blocked,
                    flagged=plan.flagged,
                    target_appraisal_exists=plan.target_appraisal_exists,
                )
            )

    return restricted, newly_appeared


def plan_counts(plans: list[AppraisalPlan]) -> dict:
    """Summary numbers for a preview, computed from the plan itself."""
    movable = [p for p in plans if p.moves]
    return {
        "appraisals": len(movable),
        "goals": sum(len(p.moves) for p in movable),
        "new_appraisals": sum(1 for p in movable if not p.target_appraisal_exists),
        "new_goals": sum(1 for p in movable for m in p.moves if not m.target_exists),
        "flagged": sum(1 for p in plans if p.is_flagged),
        "blocked": sum(len(p.blocked) for p in plans),
    }


# --- applying -------------------------------------------------------------


def apply_plan(plans: list[AppraisalPlan], to_start: int) -> dict:
    """Move every movable appraisal. Returns a before/after record.

    One transaction per appraisal, so a failure leaves that appraisal whole and
    does not stop the rest.
    """
    movable = [p for p in plans if p.moves]

    # Nothing to move: return before touching the database. Creating the target
    # year here regardless would leave a spurious empty AcademicYear behind on a
    # no-op run — and AcademicYear drives the current/previous split and
    # check_readiness, so a stray row is not free. Reachable whenever every
    # appraisal fails the re-check at confirm time, or restrict_to_approved
    # narrows the plan to nothing.
    if not movable:
        return {
            "run_at": datetime.now().isoformat(timespec="seconds"),
            "target_year": f"{to_start}/{str(to_start + 1)[-2:]}",
            "target_year_created": False,
            "appraisals": [],
            "moved_goals": 0,
            "moved_appraisals": 0,
            "failures": [],
            "notes": [],
        }

    target_year, year_created = AcademicYear.objects.get_or_create(start_year=to_start)

    record = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "target_year": str(target_year),
        "target_year_created": year_created,
        "appraisals": [],
    }
    moved = 0
    failures = []
    notes = []

    for plan in movable:
        try:
            entry, entry_notes = move_appraisal(plan, target_year)
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the rest
            failures.append((plan.appraisal.teacher.email, str(exc)))
            record["appraisals"].append(
                {"teacher_email": plan.appraisal.teacher.email, "error": str(exc)}
            )
            continue
        record["appraisals"].append(entry)
        notes.extend(entry_notes)
        moved += len(entry["goals"])

    record["moved_goals"] = moved
    record["moved_appraisals"] = len(movable) - len(failures)
    record["failures"] = [{"teacher_email": e, "error": m} for e, m in failures]
    record["notes"] = notes
    return record


def move_appraisal(plan: AppraisalPlan, target_year) -> tuple[dict, list[str]]:
    """Move one appraisal's reviews. Atomic, and re-checked under lock."""
    notes: list[str] = []
    with transaction.atomic():
        source = plan.appraisal
        target, appraisal_created = Appraisal.objects.get_or_create(
            teacher=source.teacher,
            academic_year=target_year,
            defaults={
                # Deliberately NOT copied from the source appraisal: that is a
                # snapshot of the *later* year's coach, and asserting it for an
                # earlier year would misattribute the review.
                "coach_email": "",
                # A year that predates the system should not be editable.
                "status": Appraisal.Status.SIGNED_OFF,
            },
        )

        entry = {
            "teacher_email": source.teacher.email,
            "source_appraisal_pk": source.pk,
            "target_appraisal_pk": target.pk,
            "target_appraisal_created": appraisal_created,
            "goals": [],
        }

        # Re-read under a lock: a coach may have saved the following year's
        # Last Year tab between the plan and now, and those writes land on
        # exactly these fields.
        pks = [m.goal.pk for m in plan.moves]
        fresh = Goal.objects.filter(pk__in=pks)
        if connection.features.has_select_for_update:
            fresh = fresh.select_for_update()

        for goal in fresh.order_by("order"):
            reason = edit_reason(goal)
            if reason:
                notes.append(
                    f"{source.teacher.email} goal {goal.order}: changed during "
                    f"the run ({reason}) — left untouched."
                )
                continue

            target_goal, goal_created = Goal.objects.get_or_create(
                appraisal=target,
                order=goal.order,
                defaults={"goal_type": goal.goal_type, "title": PLACEHOLDER_TITLE},
            )
            if has_review(target_goal):
                # Planned as movable, occupied by the time we got here.
                notes.append(
                    f"{source.teacher.email} goal {goal.order}: {TARGET_OCCUPIED} "
                    "— left untouched."
                )
                continue

            entry["goals"].append(
                {
                    "order": goal.order,
                    "source_goal_pk": goal.pk,
                    "target_goal_pk": target_goal.pk,
                    "target_goal_created": goal_created,
                    **{f: getattr(goal, f) for f in REVIEW_FIELDS},
                }
            )

            for name in REVIEW_FIELDS:
                setattr(target_goal, name, getattr(goal, name))
                setattr(goal, name, "")

            # Target first, source second: if the second save raises, the
            # atomic block rolls both back and no text is lost.
            target_goal.save(update_fields=list(REVIEW_FIELDS))
            goal.save(update_fields=list(REVIEW_FIELDS))

        return entry, notes


# --- helpers --------------------------------------------------------------


def has_review(goal: Goal) -> bool:
    return any(getattr(goal, name).strip() for name in REVIEW_FIELDS)


def imported_row(goal: Goal):
    """The import row that wrote this goal, most recently confirmed first.

    ``created_object_pk`` is only ever set by ``_mark_applied``, so every row
    returned here belongs to a confirmed batch.
    """
    return (
        ImportRow.objects.filter(
            import_type=ImportType.GOALS,
            created_object_model=ImportedModel.GOAL,
            created_object_pk=goal.pk,
        )
        .exclude(outcome=ImportRow.Outcome.SKIP)
        .order_by("-batch__confirmed_at", "-pk")
        .first()
    )


def edit_reason(goal: Goal) -> str:
    """Why this goal cannot be moved automatically, or "" if it can.

    Compares the stored text against the values the import wrote. A mismatch
    means a human has typed into the field since — live work that must not be
    moved. A goal with no import row was created in the app, so it is live work
    by definition.
    """
    row = imported_row(goal)
    if row is None:
        return NO_IMPORT_ROW
    for name in REVIEW_FIELDS:
        if getattr(goal, name).strip() != (row.raw_json.get(name) or "").strip():
            return TEXT_DIFFERS
    return ""

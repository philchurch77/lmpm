"""Give this year's support-staff appraisals the support wording for Goal 1.

Goal 1 used to be seeded with the teacher wording (``DEFAULT_STANDARDS_GOAL``,
"...the teacher standards are fully met...") for everyone. New support
appraisals now get ``SUPPORT_STANDARDS_GOAL``; this rewrites the ones already
created. The support wording is agreed with the client.

A title is rewritten only when ALL of these hold:

- it is Goal 1 (``order=1``, ``goal_type=STANDARDS``) of an appraisal in the
  CURRENT academic year — an earlier year's goal is history, and its review
  comments were written against the wording it had;
- its title is still EXACTLY the untouched teacher default — anything a person
  has edited, by so much as a word, is left alone;
- nobody has worked against it yet: steps, success criteria and both review
  comments are all blank (a goal someone has planned against was accepted in
  its old wording, so it is theirs to change, not ours);
- the appraisal's owner is support staff;
- the appraisal is not signed off.

Nothing else on the goal is touched. The write is a compare-and-swap on the
title, so a save that lands between the report and the write is never
overwritten. --apply requires --backup-file, outside the repository, recording
which goals were reworded (pks and years only — no text); to undo, set those
titles back to ``DEFAULT_STANDARDS_GOAL``. Reports by default; safe to re-run.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import StaffMember

from appraisals.models import (
    DEFAULT_STANDARDS_GOAL,
    SUPPORT_STANDARDS_GOAL,
    Appraisal,
    Goal,
)

# A goal with anything in these has been worked against and is left alone.
WORK_FIELDS = (
    "steps_to_success",
    "success_criteria",
    "teacher_review_comment",
    "coach_review_comment",
)


def _is_untouched(goal) -> bool:
    return not any(getattr(goal, name).strip() for name in WORK_FIELDS)


class Command(BaseCommand):
    help = (
        "Reword Goal 1 on this year's support-staff appraisals that still hold "
        "the untouched teacher default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually rewrite the titles (default is report only).",
        )
        parser.add_argument(
            "--backup-file",
            default="",
            help=(
                "Required with --apply: where to write the JSON record of which "
                "goals were reworded. Must be outside the repository."
            ),
        )

    def handle(self, *args, **options):
        backup_path = (
            self._resolve_backup_path(options["backup_file"]) if options["apply"] else None
        )

        support_goal_1 = Goal.objects.filter(
            order=1,
            goal_type=Goal.GoalType.STANDARDS,
            appraisal__academic_year__is_current=True,
            appraisal__teacher__staff_type=StaffMember.StaffType.SUPPORT,
        ).select_related("appraisal__teacher", "appraisal__academic_year")

        default_titled = list(
            support_goal_1.filter(title=DEFAULT_STANDARDS_GOAL).exclude(
                appraisal__status=Appraisal.Status.SIGNED_OFF
            )
        )
        candidates = [goal for goal in default_titled if _is_untouched(goal)]
        worked_on = len(default_titled) - len(candidates)
        signed_off = support_goal_1.filter(
            title=DEFAULT_STANDARDS_GOAL,
            appraisal__status=Appraisal.Status.SIGNED_OFF,
        ).count()
        edited = (
            support_goal_1.exclude(title=DEFAULT_STANDARDS_GOAL)
            .exclude(title=SUPPORT_STANDARDS_GOAL)
            .count()
        )

        for goal in candidates:
            self.stdout.write(
                f"  reword {goal.appraisal.teacher.email} "
                f"{goal.appraisal.academic_year} - goal pk={goal.pk}"
            )
        self.stdout.write(
            f"  left alone: {worked_on} already worked on, {edited} edited by a "
            f"person, {signed_off} signed off"
        )

        if not candidates:
            self.stdout.write(self.style.SUCCESS("Nothing to reword."))
            return

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    f"[report only] {len(candidates)} goal(s) would be reworded. "
                    "Re-run with --apply --backup-file <path outside the repo>."
                )
            )
            return

        # The record is written BEFORE the change, so a failure part-way can
        # never leave rewritten rows with no note of which they were.
        self._write_backup(
            {
                "command": "reword_support_standards_goal",
                "run_at": timezone.now().isoformat(timespec="seconds"),
                "from_title": DEFAULT_STANDARDS_GOAL,
                "to_title": SUPPORT_STANDARDS_GOAL,
                "undo": (
                    "Goal.objects.filter(pk__in=<goal_pks>, title=to_title)"
                    ".update(title=from_title)"
                ),
                "goals": [
                    {
                        "goal_pk": goal.pk,
                        "appraisal_pk": goal.appraisal_id,
                        "academic_year": str(goal.appraisal.academic_year),
                    }
                    for goal in candidates
                ],
            },
            backup_path,
        )

        with transaction.atomic():
            # Re-check the whole rule, not just the title, so a goal someone
            # started on since the report is never rewritten.
            changed = Goal.objects.filter(
                pk__in=[goal.pk for goal in candidates],
                title=DEFAULT_STANDARDS_GOAL,
                **{name: "" for name in WORK_FIELDS},
            ).update(title=SUPPORT_STANDARDS_GOAL)

        self.stdout.write(
            self.style.SUCCESS(f"Reworded {changed} goal(s). Record: {backup_path}")
        )

    def _resolve_backup_path(self, explicit) -> Path:
        if not explicit:
            raise CommandError(
                "--backup-file is required with --apply. On Azure use a path under "
                "/home/, e.g. --backup-file /home/support-goal-reword.json"
            )
        path = Path(explicit).expanduser().resolve()
        base = Path(settings.BASE_DIR).resolve()
        if path == base or base in path.parents:
            raise CommandError(
                f"--backup-file must be outside the repository ({base}): a commit "
                "of this repo deploys to production."
            )
        return path

    def _write_backup(self, record, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)

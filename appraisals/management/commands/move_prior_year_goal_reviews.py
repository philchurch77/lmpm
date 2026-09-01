"""Move goal review comments that were imported against the wrong academic year.

A one-off data correction, not part of the normal workflow. The decision rules
and the write itself live in ``appraisals.goal_review_fix`` — see that module
for the background, the shape and the safety guarantees. This command is the
command-line front end; the Django admin action on Academic years is the other,
for operators who cannot reach a console.

Use --dry-run to preview.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from appraisals.goal_review_fix import (
    DEFAULT_FROM_YEAR,
    DEFAULT_TO_YEAR,
    AdjacentYearError,
    apply_plan,
    build_plan,
    check_years,
    plan_counts,
)
from appraisals.models import AcademicYear


class Command(BaseCommand):
    help = (
        "One-off correction: move goal review comments that the bulk import "
        "attached to the wrong academic year back onto the year they review. "
        "Skips and reports any goal edited since import. Use --dry-run first."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would move without writing anything.",
        )
        parser.add_argument(
            "--from-year",
            type=int,
            default=DEFAULT_FROM_YEAR,
            help=(
                "start_year of the academic year whose goals wrongly hold the "
                f"reviews (default {DEFAULT_FROM_YEAR}, i.e. 2025/26)."
            ),
        )
        parser.add_argument(
            "--to-year",
            type=int,
            default=DEFAULT_TO_YEAR,
            help=(
                "start_year of the academic year the reviews actually describe. "
                f"Must be --from-year minus 1 (default {DEFAULT_TO_YEAR})."
            ),
        )
        parser.add_argument(
            "--teacher-email",
            default="",
            help="Move one named teacher only, to trial the correction before the full run.",
        )
        parser.add_argument(
            "--backup-file",
            default="",
            help=(
                "Where to write the JSON before/after record. Required for a real "
                "run. Must be outside the repository — it holds staff performance "
                "commentary. On Azure use a path under /home/."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        from_start = options["from_year"]
        to_start = options["to_year"]

        try:
            check_years(from_start, to_start)
        except AdjacentYearError as exc:
            raise CommandError(str(exc)) from exc

        source_year = AcademicYear.objects.filter(start_year=from_start).first()
        if source_year is None:
            raise CommandError(f"No academic year with start_year={from_start}.")

        backup_path = self._resolve_backup_path(options["backup_file"], dry_run)

        # The guard's whole purpose is protecting in-progress work in the year
        # after the source, so state which year that is rather than assuming it.
        current = AcademicYear.objects.filter(is_current=True).first()
        self.stdout.write(
            f"Source year: {source_year}   Current year: {current or 'none set'}"
        )

        plans = build_plan(source_year, to_start, options["teacher_email"])
        self._report(plans, to_start)

        counts = plan_counts(plans)
        if not counts["appraisals"]:
            self.stdout.write(self.style.SUCCESS("\nNothing to move."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\n[dry-run] would move {counts['goals']} review(s) across "
                    f"{counts['appraisals']} appraisal(s), creating "
                    f"{counts['new_appraisals']} appraisal(s) and "
                    f"{counts['new_goals']} goal(s). Nothing written."
                )
            )
            return

        record = apply_plan(plans, to_start)

        for note in record["notes"]:
            self.stdout.write(self.style.WARNING(f"  {note}"))

        self._write_backup(record, backup_path)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nMoved {record['moved_goals']} goal review(s) across "
                f"{record['moved_appraisals']} appraisal(s) into {record['target_year']}."
            )
        )
        for failure in record["failures"]:
            self.stderr.write(
                self.style.ERROR(f"  FAILED {failure['teacher_email']}: {failure['error']}")
            )
        self.stdout.write(f"Before/after record written to {backup_path}")
        self.stdout.write(
            "Keep that file somewhere controlled, and delete it once the "
            "correction is verified — it holds staff performance commentary."
        )

    # --- reporting --------------------------------------------------------

    def _report(self, plans, to_start):
        flagged = [p for p in plans if p.is_flagged]
        blocked = [p for p in plans if p.blocked]
        movable = [p for p in plans if p.moves]

        if flagged:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(flagged)} appraisal(s) SKIPPED — resolve by hand:"
                )
            )
            for plan in flagged:
                self.stdout.write(f"  {plan.appraisal.teacher.email}")
                for goal, reason in plan.flagged:
                    self.stdout.write(f"      goal {goal.order}: {reason}")
            self.stdout.write(
                "  (to move one of these after checking it, re-run with "
                "--teacher-email <address>)"
            )

        if blocked:
            self.stdout.write(
                self.style.WARNING(f"\n{len(blocked)} appraisal(s) with BLOCKED goals:")
            )
            for plan in blocked:
                for goal, reason in plan.blocked:
                    self.stdout.write(
                        f"  {plan.appraisal.teacher.email} — goal {goal.order}: {reason} "
                        "(source left as-is)"
                    )

        if movable:
            label = f"{to_start}/{str(to_start + 1)[-2:]}"
            self.stdout.write(f"\nMoving reviews back to {label}:")
            for plan in movable:
                orders = ", ".join(str(m.goal.order) for m in plan.moves)
                self.stdout.write(f"  {plan.appraisal.teacher.email} — goal(s) {orders}")

    def _resolve_backup_path(self, explicit, dry_run) -> Path | None:
        """Validate where the before-state goes. It holds personal data."""
        if dry_run:
            return None
        if not explicit:
            raise CommandError(
                "--backup-file is required for a real run. It records the text "
                "being moved, so it must sit outside the repository (a commit "
                "of this repo deploys to production). On Azure use a path under "
                "/home/, e.g. --backup-file /home/goal-review-move.json"
            )

        path = Path(explicit).expanduser().resolve()
        base = Path(settings.BASE_DIR).resolve()
        if path == base or base in path.parents:
            raise CommandError(
                f"--backup-file must be outside the repository ({base}). "
                "The file holds named staff performance commentary and would be "
                "at risk of being committed and deployed."
            )
        return path

    def _write_backup(self, record, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)

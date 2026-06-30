"""Delete line-meeting records that hold no notes (only a date).

The create flow no longer persists a meeting until the manager saves, so new
empty records should not appear. This command cleans up any legacy blanks left
by the old "create-then-fill" flow. Use --dry-run to preview first.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from line_management.models import LineMeeting


class Command(BaseCommand):
    help = "Delete line meetings with no note content (legacy blank records)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        # All five note sections blank (empty or whitespace-only). Mirrors
        # LineMeeting.is_empty; expressed as a queryset filter so the scan and
        # delete happen in the database rather than in Python.
        blank = Q()
        for field in LineMeeting.NOTE_FIELDS:
            blank &= Q(**{field: ""}) | Q(**{f"{field}__regex": r"^\s*$"})

        empties = LineMeeting.objects.filter(blank).select_related("staff")
        count = empties.count()

        if not count:
            self.stdout.write(self.style.SUCCESS("No empty line meetings found."))
            return

        for meeting in empties:
            self.stdout.write(f"  {meeting.staff.email} — {meeting.meeting_date} (pk={meeting.pk})")

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(f"[dry-run] would delete {count} empty line meeting(s).")
            )
            return

        empties.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} empty line meeting(s)."))

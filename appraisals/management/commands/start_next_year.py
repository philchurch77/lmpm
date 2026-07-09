from __future__ import annotations

from django.core.management.base import BaseCommand

from appraisals.models import AcademicYear


class Command(BaseCommand):
    help = (
        "Advance the trust to the next academic year: create the year after the "
        "latest one (if it does not exist yet) and mark it current, demoting the "
        "previously-current year. Idempotent. Backs the admin's "
        "'Start next academic year' button."
    )

    def handle(self, *args, **options):
        year, created = AcademicYear.start_next()
        verb = "Created" if created else "Made current existing"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} academic year {year} (now the current year).")
        )

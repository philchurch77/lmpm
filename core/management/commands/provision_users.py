from __future__ import annotations

from django.core.management.base import BaseCommand

from core.models import StaffMember
from core.provisioning import describe, provision_staff_members


class Command(BaseCommand):
    help = (
        "Give imported staff a login. For every StaffMember, ensure a matching "
        "Django User (by email) and a SchoolProfile (the SSO access gate) exist. "
        "Identity is by email, so the new User's email is set to the StaffMember's "
        "email; authentication is via Microsoft SSO, so the User gets an unusable "
        "local password. Idempotent — safe to re-run. Use --dry-run to preview. "
        "The same rules run automatically when a staff member is saved in the "
        "admin, and from the 'Give selected staff a login' admin action; this "
        "command remains the scriptable path for bulk work."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        # All decision rules live in core.provisioning so this command, the
        # admin action and StaffMemberAdmin.save_model cannot drift apart.
        summary = provision_staff_members(StaffMember.objects.all(), dry_run=dry_run)

        for email, outcome in summary.skipped:
            self.stdout.write(self.style.WARNING(f"  SKIP {describe(email, outcome)}"))

        verb = "Would provision" if dry_run else "Provisioned"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {summary.users_created} user(s), "
                f"{summary.profiles_created} SchoolProfile(s), "
                f"{summary.profiles_updated} school link(s) updated. "
                f"Already had login: {summary.already_ok}. "
                f"Skipped: {len(summary.skipped)}."
            )
        )

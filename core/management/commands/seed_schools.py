from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import School


class Command(BaseCommand):
    help = "Seed the initial list of schools (with demo logos). Safe to run multiple times."

    def handle(self, *args, **options):
        schools = [
            ("Rose Hill School", School.Phase.PRIMARY),
            ("Copleston High School", School.Phase.SECONDARY),
            ("Britannia Primary and Nursery School", School.Phase.PRIMARY),
            ("Stowupland High School", School.Phase.SECONDARY),
            ("Bacton Primary School", School.Phase.PRIMARY),
            ("Cedars Park Primary School", School.Phase.PRIMARY),
            ("Mendlesham Primary School", School.Phase.PRIMARY),
        ]

        demo_logos = {
            "Rose Hill School": "Rose_Hill_School.png",
            "Copleston High School": "Copleston_High_School.png",
            "Britannia Primary and Nursery School": "Britannia_Primary_and_Nursery_School.png",
            "Stowupland High School": "Stowupland_High_School.jpeg",
            "Bacton Primary School": "Bacton_Primary_School.jpeg",
            "Cedars Park Primary School": "Cedars_Park_Primary_School.jpeg",
            "Mendlesham Primary School": "Mendlesham_Primary_School.jpeg",
        }
        logos_dir = Path(settings.MEDIA_ROOT) / "school_logos"
        use_azure_media_storage = getattr(settings, "USE_AZURE_MEDIA_STORAGE", False)

        created = 0
        updated = 0
        logos_attached = 0
        for name, phase in schools:
            obj, was_created = School.objects.get_or_create(name=name)
            if was_created:
                created += 1
            if obj.phase != phase:
                obj.phase = phase
                obj.save(update_fields=["phase"])
                if not was_created:
                    updated += 1

            logo_filename = demo_logos.get(name)
            if not logo_filename:
                continue

            desired_name = f"school_logos/{logo_filename}"
            logo_path = logos_dir / logo_filename

            # Re-attach if the logo is unset or points at a file no longer on disk.
            current_missing_on_disk = False
            if obj.logo and not use_azure_media_storage:
                current_path = Path(settings.MEDIA_ROOT) / obj.logo.name
                current_missing_on_disk = not current_path.exists()

            if (not obj.logo or current_missing_on_disk) and logo_path.exists():
                obj.logo.name = desired_name
                obj.save(update_fields=["logo"])
                logos_attached += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seed complete. Schools created: {created}. Schools updated: {updated}. "
                f"Logos attached: {logos_attached}."
            )
        )

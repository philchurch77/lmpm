from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Branding


class Command(BaseCommand):
    help = "Seed Branding (trust emblem) from committed demo media assets. Safe to run multiple times."

    def handle(self, *args, **options):
        demo_filename = "thumbnail.jpg"
        demo_path = Path(settings.MEDIA_ROOT) / "branding" / demo_filename

        obj, _created = Branding.objects.get_or_create(pk=1)

        if obj.trust_emblem:
            self.stdout.write(self.style.SUCCESS("Branding already set; no changes."))
            return

        if not demo_path.exists():
            self.stdout.write(
                self.style.WARNING(
                    f"No demo branding image found at {demo_path}. Skipping. "
                    "Drop a trust emblem at media/branding/thumbnail.jpg or set it in the admin."
                )
            )
            return

        obj.trust_emblem.name = f"branding/{demo_filename}"
        obj.save(update_fields=["trust_emblem"])
        self.stdout.write(self.style.SUCCESS("Branding seeded."))

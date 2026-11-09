from django.core.management.base import BaseCommand

from attachments.links import clean_expired


class Command(BaseCommand):
    help = "Delete files uploaded for Claude previews that expired without being confirmed."

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {clean_expired()} files from expired previews.")

from django.core.management.base import BaseCommand
from django.utils import timezone

from ledger.recurring import run_all


class Command(BaseCommand):
    help = "Create and post every recurring bill and journal entry due on or before today."

    def handle(self, *args, **options):
        summary = run_all(timezone.localdate())
        self.stdout.write(f"Recurring: {summary.created} created, {summary.failed} failed.")

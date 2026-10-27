from django.core.management.base import BaseCommand

from banking.sync import sync_all


class Command(BaseCommand):
    help = "Pull new transactions from every linked bank."

    def handle(self, *args, **options):
        sync_all()
        self.stdout.write("Bank sync finished.")

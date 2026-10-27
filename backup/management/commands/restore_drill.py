from django.core.management.base import BaseCommand, CommandError

from backup.service import restore_drill


class Command(BaseCommand):
    help = "Check that the newest backup restores and matches the ledger it was taken from."

    def handle(self, *args, **options):
        result = restore_drill()
        if not result.ok:
            raise CommandError(result.message)
        self.stdout.write(result.message)

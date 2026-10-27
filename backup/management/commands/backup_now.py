from django.core.management.base import BaseCommand, CommandError

from backup.models import BackupRun
from backup.service import run_backup


class Command(BaseCommand):
    help = "Take an encrypted backup of the database now."

    def handle(self, *args, **options):
        run = run_backup()
        if run.status == BackupRun.Status.FAILED:
            raise CommandError(run.error)
        self.stdout.write(f"Backup finished: {run.filename} ({run.size_bytes:,} bytes).")

import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from backup.scheduler import next_run, parse_time


def upcoming_jobs(now):
    """Each scheduled command with its next run time after `now`."""
    backup_h, backup_m = parse_time(settings.DNB_BACKUP_TIME)
    drill_h, drill_m = parse_time(settings.DNB_DRILL_TIME)
    jobs = [
        (next_run(now, hour=backup_h, minute=backup_m), "backup_now"),
        (next_run(now, hour=drill_h, minute=drill_m, day=1), "restore_drill"),
    ]
    for entry in settings.DNB_BANK_SYNC_TIMES.split(","):
        if entry.strip():
            hour, minute = parse_time(entry.strip())
            jobs.append((next_run(now, hour=hour, minute=minute), "sync_banks"))
    return jobs


class Command(BaseCommand):
    help = "Run the nightly backup, the monthly restore drill, and the bank sync on schedule. Runs until stopped."

    def handle(self, *args, **options):
        self.stdout.write(f"Scheduler started: backup daily at {settings.DNB_BACKUP_TIME}, "
                          f"restore drill on the 1st at {settings.DNB_DRILL_TIME}, "
                          f"bank sync at {settings.DNB_BANK_SYNC_TIMES}.", ending="\n")
        while True:
            now = timezone.localtime().replace(tzinfo=None)
            when, command = min(upcoming_jobs(now))
            self.stdout.write(f"Next: {command} at {when:%Y-%m-%d %H:%M}.", ending="\n")
            self.stdout.flush()
            time.sleep(max(0.0, (when - now).total_seconds()))
            try:
                call_command(command)
            except SystemExit:
                pass  # backup_now and restore_drill exit 1 on failure; the result is on the home screen
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                self.stderr.write(f"{command} failed: {exc}")

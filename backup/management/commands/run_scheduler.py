import time

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from backup.scheduler import next_run, parse_time, wait_until
from backup.service import run_queued


def upcoming_jobs(now):
    """Each scheduled command with its next run time after `now`."""
    backup_h, backup_m = parse_time(settings.DNB_BACKUP_TIME)
    drill_h, drill_m = parse_time(settings.DNB_DRILL_TIME)
    jobs = [
        (next_run(now, hour=backup_h, minute=backup_m), "backup_now"),
        (next_run(now, hour=drill_h, minute=drill_m, day=1), "restore_drill"),
    ]
    rec_h, rec_m = parse_time(settings.DNB_RECURRING_TIME)
    jobs.append((next_run(now, hour=rec_h, minute=rec_m), "run_recurring"))
    for entry in settings.DNB_BANK_SYNC_TIMES.split(","):
        if entry.strip():
            hour, minute = parse_time(entry.strip())
            jobs.append((next_run(now, hour=hour, minute=minute), "sync_banks"))
    return jobs


class Command(BaseCommand):
    help = ("Run the nightly backup, Back up now requests, the monthly restore drill, the bank sync, and recurring "
            "bills and journal entries on schedule. Runs until stopped.")

    def handle(self, *args, **options):
        self.stdout.write(f"Scheduler started: backup daily at {settings.DNB_BACKUP_TIME}, "
                          f"restore drill on the 1st at {settings.DNB_DRILL_TIME}, "
                          f"bank sync at {settings.DNB_BANK_SYNC_TIMES}, "
                          f"recurring bills and journal entries daily at {settings.DNB_RECURRING_TIME}.", ending="\n")
        self.run_recurring_now()
        while True:
            now = timezone.localtime().replace(tzinfo=None)
            when, command = min(upcoming_jobs(now))
            self.stdout.write(f"Next: {command} at {when:%Y-%m-%d %H:%M}.", ending="\n")
            self.stdout.flush()
            wait_until(when, now=lambda: timezone.localtime().replace(tzinfo=None), sleep=time.sleep,
                       tick=self.run_requests)
            try:
                call_command(command)
            except SystemExit:
                pass  # backup_now and restore_drill exit 1 on failure; the result is on the home screen
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                self.stderr.write(f"{command} failed: {exc}")

    def run_recurring_now(self):
        """Catch up recurring documents missed while the server was off."""
        try:
            call_command("run_recurring")
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            self.stderr.write(f"run_recurring failed: {exc}")

    def run_requests(self):
        """Run a Back up now request from the home screen, if there is one."""
        try:
            run = run_queued()
        except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
            self.stderr.write(f"Back up now failed: {exc}")
            return
        if run is not None:
            self.stdout.write(f"Back up now: {run.get_status_display()}{f' ({run.error})' if run.error else ''}.")
            self.stdout.flush()

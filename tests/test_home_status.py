from datetime import timedelta

import pytest
from django.utils import timezone

from backup.models import BackupRun
from backup.service import backup_status


def make_run(status, hours_ago=1, error=""):
    t = timezone.now() - timedelta(hours=hours_ago)
    return BackupRun.objects.create(started_at=t, finished_at=t, status=status, error=error)


@pytest.fixture
def failed_run(db):
    return make_run("FAILED", error="Backup folder D:\\Drive\\dnb-backups does not exist.")


def test_status_error_after_failed_run(db):
    make_run("SUCCESS", hours_ago=3)
    make_run("FAILED", hours_ago=1, error="Backup folder X does not exist.")
    s = backup_status(timezone.now())
    assert s.level == "error" and s.message == "Last backup failed: Backup folder X does not exist."


def test_status_warning_when_stale(db):
    make_run("SUCCESS", hours_ago=27)
    s = backup_status(timezone.now())
    assert s.level == "warning" and s.message == "No successful backup in the last 26 hours."


def test_status_warning_when_never_backed_up(db):
    s = backup_status(timezone.now())
    assert s.level == "warning" and s.message == "No successful backup in the last 26 hours."


def test_status_ok_when_recent(db):
    make_run("SUCCESS", hours_ago=2)
    s = backup_status(timezone.now())
    assert s.level == "ok" and s.message.startswith("Last backup ")


def test_failure_then_recovery_shows_ok(db):
    make_run("FAILED", hours_ago=5, error="x")
    make_run("SUCCESS", hours_ago=1)
    assert backup_status(timezone.now()).level == "ok"


def test_running_backup_does_not_hide_previous_result(db):
    make_run("SUCCESS", hours_ago=2)
    BackupRun.objects.create(started_at=timezone.now(), status="RUNNING")
    assert backup_status(timezone.now()).level == "ok"


def test_home_shows_banner(client, standard_user, failed_run):
    client.force_login(standard_user)
    r = client.get("/")
    assert b"Last backup failed" in r.content and b"banner error" in r.content


def test_home_shows_warning_banner_to_admin(client, admin_user, db):
    client.force_login(admin_user)
    r = client.get("/")
    assert b"No successful backup in the last 26 hours." in r.content and b"banner warning" in r.content

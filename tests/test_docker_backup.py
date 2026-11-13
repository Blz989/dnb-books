"""Off-site backups through rclone, and the in-container scheduler.

rclone itself is replaced by tests/fake_rclone.py, which maps the remote "fake:" onto a local folder.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

from backup.models import BackupRun
from backup.scheduler import next_run
from backup.service import backup_status, run_backup, restore_drill

FAKE_RCLONE = [sys.executable, str(Path(__file__).with_name("fake_rclone.py"))]


@pytest.fixture
def remote(tmp_path, settings, monkeypatch):
    folder = tmp_path / "drive"
    folder.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr("backup.remote.time.sleep", lambda s: None)   # no real pause between upload attempts
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(folder))
    settings.DNB_RCLONE = FAKE_RCLONE
    settings.DNB_BACKUP_REMOTE = "fake:dnb-backups"
    settings.DNB_BACKUP_DIR = local
    return folder / "dnb-backups"


def test_backup_uploads_to_remote(file_db_with_entries, remote):
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS
    assert (remote / run.filename).exists()


def test_failed_upload_marks_backup_failed(file_db_with_entries, remote, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "Failed to copy: couldn't connect to Google Drive")
    run = run_backup()
    assert run.status == BackupRun.Status.FAILED
    assert run.error == "Backup saved locally but not uploaded: Failed to copy: couldn't connect to Google Drive"
    status = backup_status(run.finished_at)
    assert status.level == "error" and "not uploaded" in status.message


def test_retention_also_removes_old_remote_copies(file_db_with_entries, remote, settings):
    remote.mkdir(parents=True)
    old = "dnb-20200105-011500.sqlite3.enc"
    newer_same_month = "dnb-20200120-011500.sqlite3.enc"
    for name in (old, newer_same_month):
        (Path(settings.DNB_BACKUP_DIR) / name).write_bytes(b"x")
        (remote / name).write_bytes(b"x")
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS
    assert not (remote / old).exists() and (remote / newer_same_month).exists()


def test_no_remote_configured_keeps_local_only(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_REMOTE = ""
    settings.DNB_BACKUP_DIR = tmp_path
    assert run_backup().status == BackupRun.Status.SUCCESS


def test_restore_drill_checks_the_remote_copy(file_db_with_entries, remote, settings):
    run = run_backup()
    (Path(settings.DNB_BACKUP_DIR) / run.filename).unlink()   # local copy gone; the remote one must be used
    result = restore_drill()
    assert result.ok, result.message


def test_restore_drill_fails_when_remote_copy_missing(file_db_with_entries, remote):
    run = run_backup()
    (remote / run.filename).unlink()
    result = restore_drill()
    assert not result.ok
    assert result.message.startswith(f"Restore drill failed: couldn't download {run.filename} from fake:dnb-backups")


@pytest.mark.parametrize("now,kwargs,expected", [
    (datetime(2026, 10, 27, 0, 0), {"hour": 1, "minute": 15}, datetime(2026, 10, 27, 1, 15)),
    (datetime(2026, 10, 27, 1, 15), {"hour": 1, "minute": 15}, datetime(2026, 10, 28, 1, 15)),
    (datetime(2026, 10, 27, 9, 0), {"hour": 2, "minute": 15, "day": 1}, datetime(2026, 11, 1, 2, 15)),
    (datetime(2026, 12, 5, 9, 0), {"hour": 2, "minute": 15, "day": 1}, datetime(2027, 1, 1, 2, 15)),
    (datetime(2026, 11, 1, 2, 0), {"hour": 2, "minute": 15, "day": 1}, datetime(2026, 11, 1, 2, 15)),
])
def test_next_run(now, kwargs, expected):
    assert next_run(now, **kwargs) == expected


# --- upload retries (rclone mocked; never touches Drive) ---

import subprocess

from backup import remote as remote_module


def _fake_run(outcomes, calls):
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        outcome = outcomes[len(calls) - 1]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, outcome, "", "" if outcome == 0 else "2026/11/13 01:15:00 ERROR : boom")
    return run


@pytest.fixture
def mocked_rclone(file_db_with_entries, tmp_path, settings, monkeypatch):
    settings.DNB_BACKUP_REMOTE = "gdrive:dnb-backups"
    settings.DNB_BACKUP_DIR = tmp_path
    sleeps = []
    monkeypatch.setattr(remote_module.time, "sleep", sleeps.append)
    calls = []

    def install(outcomes):
        monkeypatch.setattr(remote_module.subprocess, "run", _fake_run(outcomes, calls))
        return calls, sleeps
    return install


def test_upload_first_attempt_succeeds_with_one_call(mocked_rclone):
    calls, sleeps = mocked_rclone([0, 0])  # copy, then retention delete (none)
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS and run.error == ""
    copies = [c for c, _ in calls if "copy" in c]
    assert len(copies) == 1 and sleeps == []
    assert calls[0][1]["timeout"] == 180 and "--timeout" in calls[0][0]


def test_stalled_upload_is_retried_and_succeeds(mocked_rclone):
    calls, sleeps = mocked_rclone(["timeout", 0])
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS and run.error == ""
    assert len([c for c, _ in calls if "copy" in c]) == 2 and len(sleeps) == 1


def test_upload_gives_up_after_three_attempts_and_keeps_local_file(mocked_rclone, tmp_path):
    calls, sleeps = mocked_rclone(["timeout", "timeout", "timeout"])
    run = run_backup()
    assert run.status == BackupRun.Status.FAILED
    assert run.error == "Backup saved locally but not uploaded: rclone took longer than 3 minutes."
    assert len(calls) == 3 and len(sleeps) == 2
    assert (tmp_path / run.filename).exists()
    assert "not uploaded" in backup_status(run.finished_at).message


def test_upload_retries_after_an_rclone_error_too(mocked_rclone):
    calls, _ = mocked_rclone([1, 1, 1])
    run = run_backup()
    assert run.error == "Backup saved locally but not uploaded: boom"
    assert len(calls) == 3

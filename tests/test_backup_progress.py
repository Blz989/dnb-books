"""Manual backups with live progress (plan 2026-11-24-manual-backup-progress)."""
import sys
from pathlib import Path

import pytest

from backup import remote

FAKE_RCLONE = [sys.executable, str(Path(__file__).with_name("fake_rclone.py"))]


@pytest.fixture
def fake_remote(tmp_path, settings, monkeypatch):
    folder = tmp_path / "drive"
    folder.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(folder))
    settings.DNB_RCLONE = FAKE_RCLONE
    settings.DNB_BACKUP_REMOTE = "fake:dnb-backups"
    settings.DNB_BACKUP_DIR = local
    return folder / "dnb-backups"


# ---- Task 1: streamed rclone upload ----

def test_upload_reports_progress_and_retries(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_RETRIES", "3")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x" * 4000)
    seen = []
    remote.upload(src, on_progress=seen.append)
    assert (fake_remote / "file.bin").exists()
    assert [p.bytes_done for p in seen] == [1000, 2000, 3000, 4000]
    assert all(p.bytes_total == 4000 for p in seen)
    assert seen[-1].retries == 3
    assert "Rate Limit Exceeded" in seen[-1].last_retry


def test_upload_without_callback_still_works(fake_remote, tmp_path):
    src = tmp_path / "file.bin"
    src.write_bytes(b"abc")
    remote.upload(src)
    assert (fake_remote / "file.bin").read_bytes() == b"abc"


def test_upload_timeout_says_how_far_it_got(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "TIMEOUT_SECONDS", 1.5)
    monkeypatch.setenv("FAKE_RCLONE_SLOW", "0.6")
    monkeypatch.setenv("FAKE_RCLONE_RETRIES", "2")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x" * 4_000_000)
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload(src)
    message = str(exc.value)
    assert message.startswith("rclone took longer than ")
    assert "It sent 2.0 MB of 4.0 MB." in message
    assert message.endswith("Google asked it to slow down 2 times.")
    assert not (fake_remote / "file.bin").exists()


def test_upload_error_text_from_json_log(fake_remote, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "Failed to copy: couldn't connect to Google Drive")
    src = tmp_path / "file.bin"
    src.write_bytes(b"x")
    with pytest.raises(remote.RemoteError) as exc:
        remote.upload(src)
    assert str(exc.value) == "Failed to copy: couldn't connect to Google Drive"


def test_upload_missing_rclone(settings, tmp_path):
    settings.DNB_RCLONE = [str(tmp_path / "no-such-rclone")]
    settings.DNB_BACKUP_REMOTE = "fake:x"
    src = tmp_path / "file.bin"
    src.write_bytes(b"x")
    with pytest.raises(remote.RemoteError, match="rclone is not installed."):
        remote.upload(src)


@pytest.mark.parametrize("n,text", [(0, "0 bytes"), (900, "900 bytes"), (12_400_000, "12.4 MB"), (2_500_000_000, "2.5 GB")])
def test_size_text(n, text):
    assert remote.size_text(n) == text


# ---- Task 2: steps, progress, and timings ----

from backup.models import BackupRun  # noqa: E402
from backup.service import run_backup  # noqa: E402


def test_run_records_each_step_with_its_time(file_db_with_entries, fake_remote):
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS
    assert [s for s, _ in run.step_times] == ["check", "snapshot", "archive", "encrypt", "upload", "cleanup"]
    assert all(seconds >= 0 for _, seconds in run.step_times)
    assert run.progress == 100 and run.step == ""
    assert run.updated_at is not None


def test_run_without_remote_skips_upload_step(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_REMOTE = ""
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    assert [s for s, _ in run.step_times] == ["check", "snapshot", "archive", "encrypt", "cleanup"]


def test_failed_run_keeps_the_step_it_stopped_in(file_db_with_entries, fake_remote, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "Failed to copy: couldn't connect to Google Drive")
    run = run_backup()
    assert run.status == BackupRun.Status.FAILED
    assert run.step == "upload"
    assert run.step_times[-1][0] == "upload"


def test_upload_progress_is_saved_while_running(file_db_with_entries, fake_remote, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_RETRIES", "2")
    saved = []
    original = BackupRun.save

    def spy(self, *args, **kwargs):
        if self.step == "upload":
            saved.append((self.progress, self.detail))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BackupRun, "save", spy)
    monkeypatch.setattr("backup.service.SAVE_EVERY_SECONDS", 0)
    run_backup()
    details = [d for _, d in saved if d]
    assert any(" of " in d and "slow down 2 times" in d for d in details), details
    percents = [p for p, _ in saved]
    assert percents == sorted(percents) and max(percents) <= 95


def test_backup_now_prints_each_step(file_db_with_entries, tmp_path, settings, capsys):
    from django.core.management import call_command
    settings.DNB_BACKUP_REMOTE = ""
    settings.DNB_BACKUP_DIR = tmp_path
    call_command("backup_now")
    out = capsys.readouterr().out
    assert "Copying the database" in out and "Backup finished" in out

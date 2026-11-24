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


# ---- Task 3: queue, scheduler pickup, stuck runs ----

from datetime import datetime, timedelta  # noqa: E402

from django.utils import timezone  # noqa: E402

from backup import service  # noqa: E402
from backup.scheduler import POLL_SECONDS, wait_until  # noqa: E402


def test_request_queues_one_backup_and_refuses_a_second(db, admin_user):
    run = service.request_backup(admin_user)
    assert run.status == BackupRun.Status.QUEUED and run.requested_by == admin_user
    with pytest.raises(service.BackupError, match="^A backup is already running.$"):
        service.request_backup(admin_user)


def test_request_refused_while_nightly_backup_runs(db, admin_user):
    BackupRun.objects.create(started_at=timezone.now(), status=BackupRun.Status.RUNNING, updated_at=timezone.now())
    with pytest.raises(service.BackupError, match="already running"):
        service.request_backup(admin_user)


def test_run_queued_runs_the_request(file_db_with_entries, tmp_path, settings, admin_user):
    settings.DNB_BACKUP_REMOTE = ""
    settings.DNB_BACKUP_DIR = tmp_path
    queued = service.request_backup(admin_user)
    done = service.run_queued()
    assert done.pk == queued.pk and done.status == BackupRun.Status.SUCCESS
    assert service.run_queued() is None


def test_stuck_running_backup_is_marked_failed(db, admin_user):
    now = timezone.now()
    old = BackupRun.objects.create(started_at=now - timedelta(hours=1), status=BackupRun.Status.RUNNING,
                                   updated_at=now - timedelta(minutes=21))
    legacy = BackupRun.objects.create(started_at=now - timedelta(days=3), status=BackupRun.Status.RUNNING)
    service.expire_stuck_runs(now)
    for run in (old, legacy):
        run.refresh_from_db()
        assert run.status == BackupRun.Status.FAILED
        assert run.error == "The backup stopped without finishing. The server may have restarted during it."
    service.request_backup(admin_user)  # no longer blocked


def test_request_the_scheduler_never_started_is_marked_failed(db, admin_user):
    now = timezone.now()
    run = service.request_backup(admin_user, now=now - timedelta(minutes=11))
    service.expire_stuck_runs(now)
    run.refresh_from_db()
    assert run.status == BackupRun.Status.FAILED
    assert run.error == "The scheduler didn't start this backup. Check that the scheduler service is running."


def test_recent_running_backup_is_left_alone(db):
    now = timezone.now()
    run = BackupRun.objects.create(started_at=now - timedelta(minutes=30), status=BackupRun.Status.RUNNING,
                                   updated_at=now - timedelta(seconds=5))
    service.expire_stuck_runs(now)
    run.refresh_from_db()
    assert run.status == BackupRun.Status.RUNNING


def test_scheduler_checks_for_requests_every_few_seconds():
    clock = [datetime(2026, 11, 24, 10, 0, 0)]
    sleeps, ticks = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    wait_until(clock[0] + timedelta(seconds=12), now=lambda: clock[0], sleep=sleep, tick=lambda: ticks.append(1))
    assert sleeps == [POLL_SECONDS, POLL_SECONDS, 2] and POLL_SECONDS == 5
    assert len(ticks) == 3


# ---- Task 4: button, progress bar, status JSON ----


def test_admin_sees_back_up_now_button(client, admin_user):
    client.force_login(admin_user)
    html = client.get("/").content.decode()
    assert "Back up now" in html and 'action="/backup/run/"' in html


def test_standard_user_has_no_button_and_cannot_post(client, standard_user):
    client.force_login(standard_user)
    assert 'action="/backup/run/"' not in client.get("/").content.decode()
    assert client.post("/backup/run/").status_code == 403
    assert not BackupRun.objects.exists()


def test_post_queues_backup_and_shows_progress(client, admin_user):
    client.force_login(admin_user)
    response = client.post("/backup/run/", follow=True)
    assert BackupRun.objects.get().status == BackupRun.Status.QUEUED
    html = response.content.decode()
    assert "Waiting to start" in html and 'class="backup-progress"' in html
    assert "Back up now</button>" not in html


def test_second_post_shows_already_running(client, admin_user):
    client.force_login(admin_user)
    client.post("/backup/run/")
    html = client.post("/backup/run/", follow=True).content.decode()
    assert "A backup is already running." in html
    assert BackupRun.objects.count() == 1


def test_get_is_not_allowed(client, admin_user):
    client.force_login(admin_user)
    assert client.get("/backup/run/").status_code == 405


def test_status_json_reports_step_percent_and_detail(client, standard_user):
    client.force_login(standard_user)
    assert client.get("/backup/status/").json() == {"active": False}
    BackupRun.objects.create(started_at=timezone.now(), status=BackupRun.Status.RUNNING, updated_at=timezone.now(),
                             step="upload", progress=47, detail="12.4 MB of 40.1 MB.",
                             step_times=[["check", 0.1], ["snapshot", 0.4], ["archive", 1.0], ["encrypt", 0.5]])
    data = client.get("/backup/status/").json()
    assert data["active"] is True
    assert data["progress"] == 47
    assert data["label"] == "Step 5 of 6: Uploading the off-site copy"
    assert data["detail"] == "12.4 MB of 40.1 MB."


def test_status_json_says_when_scheduler_is_slow_to_start(client, admin_user):
    client.force_login(admin_user)
    service.request_backup(admin_user, now=timezone.now() - timedelta(seconds=90))
    data = client.get("/backup/status/").json()
    assert data["label"] == "Waiting to start"
    assert data["detail"] == "Waiting for the scheduler to start it."


def test_status_requires_login(client):
    assert client.get("/backup/status/").status_code == 302


def test_card_shows_last_run_time_and_slowest_step(client, admin_user):
    now = timezone.now()
    BackupRun.objects.create(started_at=now - timedelta(seconds=75), finished_at=now, updated_at=now,
                             status=BackupRun.Status.SUCCESS, filename="dnb-x.tar.enc", progress=100,
                             step_times=[["check", 0.1], ["snapshot", 2.0], ["upload", 58.2], ["cleanup", 1.0]])
    client.force_login(admin_user)
    html = client.get("/").content.decode()
    assert "Took 1m 15s. Slowest step: Uploading the off-site copy, 58s." in html


def test_card_shows_where_a_failed_run_stopped(client, admin_user):
    now = timezone.now()
    BackupRun.objects.create(started_at=now - timedelta(minutes=16), finished_at=now, updated_at=now,
                             status=BackupRun.Status.FAILED, step="upload",
                             error="Backup saved locally but not uploaded: rclone took longer than 15 minutes.",
                             step_times=[["check", 0.1], ["upload", 900.0]])
    client.force_login(admin_user)
    html = client.get("/").content.decode()
    assert "Stopped during Uploading the off-site copy after 15m 0s." in html


def test_home_shows_a_stuck_backup_as_failed_without_writing(client, admin_user):
    now = timezone.now()
    run = BackupRun.objects.create(started_at=now - timedelta(hours=1), status=BackupRun.Status.RUNNING,
                                   updated_at=now - timedelta(minutes=25), step="upload")
    client.force_login(admin_user)
    html = client.get("/").content.decode()
    assert "Last backup failed: The backup stopped without finishing." in html
    assert 'class="backup-progress"' not in html and "Back up now</button>" in html
    run.refresh_from_db()
    assert run.status == BackupRun.Status.RUNNING  # left for the scheduler to mark failed


def test_scheduler_tick_marks_stuck_runs_failed(db):
    now = timezone.now()
    run = BackupRun.objects.create(started_at=now - timedelta(hours=1), status=BackupRun.Status.RUNNING,
                                   updated_at=now - timedelta(minutes=25))
    assert service.run_queued() is None
    run.refresh_from_db()
    assert run.status == BackupRun.Status.FAILED

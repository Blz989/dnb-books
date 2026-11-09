"""Uploaded files (attachments and the logo) are backed up with the database and checked by the restore drill."""
import hashlib
import io
import os
import sys
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from backup.models import BackupRun
from backup.service import (
    MEDIA_DIRNAME,
    list_media,
    manifest_filename,
    read_manifest,
    restore_drill,
    run_backup,
)

FAKE_RCLONE = [sys.executable, str(Path(__file__).with_name("fake_rclone.py"))]


def _write(root: Path, rel: str, data: bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def media(settings):
    root = Path(settings.MEDIA_ROOT)
    _write(root, "logo/logo.png", b"\x89PNG\r\n\x1a\nlogo")
    _write(root, "attachments/2026/11/abc.pdf", b"%PDF-1.4 bill")
    return root


@pytest.fixture
def local_backups(tmp_path, settings):
    folder = tmp_path / "backups"
    folder.mkdir()
    settings.DNB_BACKUP_DIR = folder
    return folder


@pytest.fixture
def remote(tmp_path, settings, monkeypatch, local_backups):
    drive = tmp_path / "drive"
    drive.mkdir()
    monkeypatch.setenv("FAKE_RCLONE_ROOT", str(drive))
    settings.DNB_RCLONE = FAKE_RCLONE
    settings.DNB_BACKUP_REMOTE = "fake:dnb-backups"
    return drive / "dnb-backups"


def test_list_media_finds_files_with_size_and_hash(media):
    files = list_media()
    assert [f.path for f in files] == ["attachments/2026/11/abc.pdf", "logo/logo.png"]
    pdf = files[0]
    assert pdf.size == len(b"%PDF-1.4 bill")
    assert pdf.sha256 == hashlib.sha256(b"%PDF-1.4 bill").hexdigest()


def test_list_media_skips_temporary_uploads(media):
    _write(media, "tmp/upload-123", b"half written")
    assert "tmp/upload-123" not in [f.path for f in list_media()]


def test_list_media_empty_or_missing(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "nothing-here"
    assert list_media() == []


def test_first_backup_encrypts_every_file_then_only_new_ones(file_db_with_entries, media, local_backups):
    first = run_backup()
    assert first.status == BackupRun.Status.SUCCESS, first.error
    assert (first.media_files, first.media_new) == (2, 2)
    assert first.media_bytes == sum(f.size for f in list_media())
    stored = sorted((local_backups / MEDIA_DIRNAME).iterdir())
    assert len(stored) == 2
    assert all(not p.read_bytes().startswith(b"%PDF") for p in stored)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in stored}

    _write(media, "attachments/2026/11/def.jpg", b"\xff\xd8\xffphoto")
    for p in stored:
        os.utime(p, ns=(1, 1))  # a rewrite would change this
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in stored}
    second = run_backup()
    assert second.status == BackupRun.Status.SUCCESS, second.error
    assert (second.media_files, second.media_new) == (3, 1)
    for name, (mtime, data) in before.items():
        p = local_backups / MEDIA_DIRNAME / name
        assert (p.stat().st_mtime_ns, p.read_bytes()) == (mtime, data)


def test_manifest_lists_every_file(file_db_with_entries, media, local_backups, settings):
    run = run_backup()
    assert run.media_manifest == manifest_filename(run.filename[len("dnb-"):-len(".sqlite3.enc")])
    listed = read_manifest(local_backups / run.media_manifest, settings.DNB_BACKUP_KEY)
    assert listed == list_media()


def test_backup_with_no_files_still_writes_a_manifest(file_db_with_entries, local_backups, settings):
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS
    assert (run.media_files, run.media_new, run.media_bytes) == (0, 0, 0)
    assert read_manifest(local_backups / run.media_manifest, settings.DNB_BACKUP_KEY) == []


def test_files_and_manifest_go_off_site(file_db_with_entries, media, remote):
    run = run_backup()
    assert run.status == BackupRun.Status.SUCCESS, run.error
    assert (remote / run.media_manifest).exists()
    assert len(list((remote / MEDIA_DIRNAME).iterdir())) == 2


def test_retention_never_deletes_media_and_drops_old_manifests(file_db_with_entries, media, remote, local_backups):
    from datetime import datetime, timedelta

    from django.utils import timezone

    old = timezone.make_aware(datetime(2026, 1, 3, 1, 15))
    newer = timezone.make_aware(datetime(2026, 1, 4, 1, 15))
    first = run_backup(now=old)
    run_backup(now=newer)
    run_backup(now=old + timedelta(days=90))
    assert not (local_backups / first.media_manifest).exists()  # same day as a deleted database file
    assert not (remote / first.media_manifest).exists()
    assert len(list((local_backups / MEDIA_DIRNAME).iterdir())) == 2
    assert len(list((remote / MEDIA_DIRNAME).iterdir())) == 2


def test_media_write_failure_fails_the_run(file_db_with_entries, media, local_backups):
    (local_backups / MEDIA_DIRNAME).write_text("not a folder")
    run = run_backup()
    assert run.status == BackupRun.Status.FAILED
    assert run.error.startswith("Backup saved the database but not the files: ")


def test_media_upload_failure_fails_the_run(file_db_with_entries, media, remote, monkeypatch):
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "couldn't connect to Google Drive")
    run = run_backup()
    assert run.status == BackupRun.Status.FAILED
    assert "couldn't connect to Google Drive" in run.error


def test_drill_passes_and_checks_files(file_db_with_entries, media, remote):
    run = run_backup()
    result = restore_drill()
    assert result.ok, result.message
    run.refresh_from_db()
    assert run.drill_passed_at is not None


def test_drill_fails_when_an_offsite_file_is_missing(file_db_with_entries, media, remote):
    run_backup()
    victim = sorted((remote / MEDIA_DIRNAME).iterdir())[0]
    victim.unlink()
    result = restore_drill()
    assert not result.ok
    assert "1 files in dnb-" in result.message and "are missing or don't match" in result.message


def test_drill_fails_when_an_offsite_file_was_altered(file_db_with_entries, media, remote, settings):
    from cryptography.fernet import Fernet

    run_backup()
    victim = sorted((remote / MEDIA_DIRNAME).iterdir())[0]
    victim.write_bytes(Fernet(settings.DNB_BACKUP_KEY.encode()).encrypt(b"other bytes"))
    result = restore_drill()
    assert not result.ok and "are missing or don't match" in result.message


def test_drill_checks_local_copies_when_off_site_is_off(file_db_with_entries, media, local_backups):
    run_backup()
    sorted((local_backups / MEDIA_DIRNAME).iterdir())[0].unlink()
    result = restore_drill()
    assert not result.ok and "are missing or don't match" in result.message


def test_drill_on_a_backup_made_before_file_backups(file_db_with_entries, local_backups):
    run = run_backup()
    BackupRun.objects.filter(pk=run.pk).update(media_manifest="", media_files=None, media_new=None, media_bytes=None)
    result = restore_drill()
    assert result.ok
    assert result.message == (f"Restore drill passed: {run.filename} restores and its ledger matches. "
                              "It was made before file backups, so files weren't checked.")


def test_restore_backup_brings_back_the_files(file_db_with_entries, media, local_backups, tmp_path):
    run = run_backup()
    target = tmp_path / "restored.sqlite3"
    folder = tmp_path / "restored-media"
    out = io.StringIO()
    call_command("restore_backup", run.filename, "--to", str(target), "--media-to", str(folder), stdout=out)
    for f in list_media():
        assert (folder / f.path).read_bytes() == (media / f.path).read_bytes()
    assert f"Move {folder} to" in out.getvalue()


def test_restore_backup_refuses_an_existing_media_folder(file_db_with_entries, media, local_backups, tmp_path):
    run = run_backup()
    folder = tmp_path / "exists"
    folder.mkdir()
    with pytest.raises(CommandError, match="already exists"):
        call_command("restore_backup", run.filename, "--to", str(tmp_path / "r.sqlite3"), "--media-to", str(folder))

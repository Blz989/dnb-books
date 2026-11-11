import io
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from backup.models import BackupRun
import tarfile

from backup.service import (
    BackupError, FILENAME_RE, read_snapshot_bytes, restore_archive,
    apply_retention, backup_filename, decrypt_file, encrypt_file, ledger_fingerprint, run_backup, snapshot,
)
from ledger.posting import LineSpec, post_entry


def test_encrypt_roundtrip(tmp_path):
    key = Fernet.generate_key().decode()
    src, enc, out = tmp_path / "a.bin", tmp_path / "a.enc", tmp_path / "a.out"
    src.write_bytes(b"SQLite format 3\x00 secret ledger")
    encrypt_file(src, enc, key)
    assert b"secret ledger" not in enc.read_bytes()
    decrypt_file(enc, out, key)
    assert out.read_bytes() == src.read_bytes()


def test_decrypt_with_wrong_key_fails(tmp_path):
    src, enc = tmp_path / "a.bin", tmp_path / "a.enc"
    src.write_bytes(b"data")
    encrypt_file(src, enc, Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        decrypt_file(enc, tmp_path / "out", Fernet.generate_key().decode())


def test_snapshot_is_a_valid_copy(file_db_with_entries, tmp_path):
    out = snapshot(tmp_path / "snap.sqlite3")
    conn = sqlite3.connect(out)
    assert conn.execute("SELECT COUNT(*) FROM ledger_journalentry").fetchone()[0] == 3
    conn.close()


def test_fingerprint_changes_with_posted_entries_only(file_db_with_entries, settings, admin_user, cash, revenue):
    def fp():
        conn = sqlite3.connect(settings.DNB_DB_PATH)
        try:
            return ledger_fingerprint(conn)
        finally:
            conn.close()

    before = fp()
    assert len(before) == 64 and fp() == before
    from ledger.models import JournalEntry
    JournalEntry.objects.create(date=date(2026, 1, 9), created_by=admin_user)  # draft: no effect
    assert fp() == before
    post_entry(date=date(2026, 1, 9), user=admin_user, lines=[
        LineSpec(cash, debit=Decimal("1.00")), LineSpec(revenue, credit=Decimal("1.00"))])
    assert fp() != before


def test_run_backup_success_writes_encrypted_file(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    assert run.status == "SUCCESS" and (tmp_path / run.filename).exists()
    assert not (tmp_path / run.filename).read_bytes().startswith(b"SQLite format 3")
    assert run.size_bytes == (tmp_path / run.filename).stat().st_size
    assert run.finished_at is not None and len(run.ledger_fingerprint) == 64
    assert not list(tmp_path.glob("*.part"))


def test_backup_decrypts_to_matching_database(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    restored = tmp_path / "restored.sqlite3"
    restore_archive(tmp_path / run.filename, restored, None, settings.DNB_BACKUP_KEY)
    conn = sqlite3.connect(restored)
    assert ledger_fingerprint(conn) == run.ledger_fingerprint
    conn.close()


def test_missing_backup_dir_records_failure(file_db_with_entries, tmp_path, settings):  # Review Focus 4
    settings.DNB_BACKUP_DIR = tmp_path / "gone"
    run = run_backup()
    assert run.status == "FAILED" and "does not exist" in run.error
    assert str(tmp_path / "gone") in run.error
    assert BackupRun.objects.get(pk=run.pk).status == "FAILED"


def test_missing_key_records_failure(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    settings.DNB_BACKUP_KEY = ""
    run = run_backup()
    assert run.status == "FAILED" and run.error == "No backup key set. Run generate_backup_key."
    assert list(tmp_path.iterdir()) == []


def test_invalid_key_records_failure(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    settings.DNB_BACKUP_KEY = "not-a-key"
    run = run_backup()
    assert run.status == "FAILED" and "key" in run.error.lower()


def test_retention(tmp_path):
    today = date(2026, 10, 27)
    names = []
    for back in range(40):
        d = date.fromordinal(today.toordinal() - back)
        names.append(f"dnb-{d:%Y%m%d}-011500.sqlite3.enc")
    names += [
        "dnb-20260810-011500.sqlite3.enc", "dnb-20260825-011500.sqlite3.enc",
        "dnb-20260705-011500.sqlite3.enc", "dnb-20260720-011500.sqlite3.enc",
        "notes.txt", "dnb-notes.sqlite3.enc", "dnb-20260101-011500.sqlite3.enc.part",
    ]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    deleted = apply_retention(tmp_path, today)
    kept = {p.name for p in tmp_path.iterdir()}
    daily = {f"dnb-{date.fromordinal(today.toordinal() - b):%Y%m%d}-011500.sqlite3.enc" for b in range(30)}
    assert kept == daily | {
        "dnb-20260825-011500.sqlite3.enc", "dnb-20260720-011500.sqlite3.enc",
        "notes.txt", "dnb-notes.sqlite3.enc", "dnb-20260101-011500.sqlite3.enc.part",
    }
    assert {p.name for p in deleted} == set(names) - kept


def test_run_backup_applies_retention(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    (tmp_path / "dnb-20240101-011500.sqlite3.enc").write_bytes(b"x")
    (tmp_path / "dnb-20240102-011500.sqlite3.enc").write_bytes(b"x")
    run_backup(now=timezone.make_aware(datetime(2026, 10, 27, 1, 15)))
    names = {p.name for p in tmp_path.iterdir()}
    assert "dnb-20240102-011500.sqlite3.enc" in names and "dnb-20240101-011500.sqlite3.enc" not in names


def test_backup_now_command_success_and_failure(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    out = io.StringIO()
    call_command("backup_now", stdout=out)
    assert "Backup finished" in out.getvalue()
    settings.DNB_BACKUP_DIR = tmp_path / "gone"
    with pytest.raises(CommandError, match="does not exist"):
        call_command("backup_now")


def test_generate_backup_key_prints_valid_key_and_instruction():
    out = io.StringIO()
    call_command("generate_backup_key", stdout=out)
    text = out.getvalue()
    Fernet(text.splitlines()[0].strip().encode())
    assert "Add this to .env as DNB_BACKUP_KEY and store a copy in your password manager. Backups cannot be restored without it." in text


# ---- Task 6: uploaded files are in the backup ----


def _media(settings, tmp_path):
    media = tmp_path / "media"
    (media / "reconciliation").mkdir(parents=True)
    (media / "logo.png").write_bytes(b"\x89PNG logo bytes")
    (media / "reconciliation" / "rec-1.pdf").write_bytes(b"%PDF-1.4 report \x00\xff")
    settings.MEDIA_ROOT = media
    return media


def _plain_tar(settings, tmp_path, members):
    """An encrypted archive with exactly these members: (name, bytes) or (name, TarInfo)."""
    import io as _io
    buf = _io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in members:
            if isinstance(payload, tarfile.TarInfo):
                payload.name = name
                tar.addfile(payload)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, _io.BytesIO(payload))
    path = tmp_path / "plain.tar"
    path.write_bytes(buf.getvalue())
    out = tmp_path / "dnb-20261101-010000.tar.enc"
    encrypt_file(path, out, settings.DNB_BACKUP_KEY)
    return out


def test_new_backup_name_and_both_names_match():
    assert backup_filename(datetime(2026, 10, 27, 1, 15, 3)) == "dnb-20261027-011503.tar.enc"
    assert FILENAME_RE.match("dnb-20261027-011503.tar.enc")
    assert FILENAME_RE.match("dnb-20261027-011503.sqlite3.enc")
    assert not FILENAME_RE.match("dnb-20261027-011503.tar.enc.part")


def test_backup_restores_logo_and_report_byte_for_byte(file_db_with_entries, tmp_path, settings):
    backups = tmp_path / "backups"
    backups.mkdir()
    settings.DNB_BACKUP_DIR = backups
    media = _media(settings, tmp_path)
    run = run_backup()
    assert run.status == "SUCCESS" and run.filename.endswith(".tar.enc")
    db_out, media_out = tmp_path / "r.sqlite3", tmp_path / "r-media"
    restore_archive(backups / run.filename, db_out, media_out, settings.DNB_BACKUP_KEY)
    assert (media_out / "logo.png").read_bytes() == (media / "logo.png").read_bytes()
    assert (media_out / "reconciliation" / "rec-1.pdf").read_bytes() == (media / "reconciliation" / "rec-1.pdf").read_bytes()
    conn = sqlite3.connect(db_out)
    assert ledger_fingerprint(conn) == run.ledger_fingerprint
    conn.close()


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_backup_works_without_media_files(file_db_with_entries, tmp_path, settings, kind):
    settings.DNB_BACKUP_DIR = tmp_path / "b"
    settings.DNB_BACKUP_DIR.mkdir()
    settings.MEDIA_ROOT = tmp_path / "media"
    if kind == "empty":
        settings.MEDIA_ROOT.mkdir()
    run = run_backup()
    assert run.status == "SUCCESS", run.error
    out = tmp_path / "m"
    restore_archive(settings.DNB_BACKUP_DIR / run.filename, tmp_path / "r.sqlite3", out, settings.DNB_BACKUP_KEY)
    assert out.is_dir() and not any(out.iterdir())


def test_symlinks_in_media_are_not_followed(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path / "b"
    settings.DNB_BACKUP_DIR.mkdir()
    media = _media(settings, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not back up")
    (media / "link.txt").symlink_to(secret)
    (media / "linkdir").symlink_to(tmp_path / "b", target_is_directory=True)
    run = run_backup()
    assert run.status == "SUCCESS", run.error
    out = tmp_path / "m"
    restore_archive(settings.DNB_BACKUP_DIR / run.filename, tmp_path / "r.sqlite3", out, settings.DNB_BACKUP_KEY)
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == ["logo.png", "rec-1.pdf"]


def test_media_file_vanishing_during_backup_does_not_fail(file_db_with_entries, tmp_path, settings, monkeypatch):
    settings.DNB_BACKUP_DIR = tmp_path / "b"
    settings.DNB_BACKUP_DIR.mkdir()
    media = _media(settings, tmp_path)
    real_open = Path.read_bytes

    def flaky(self):
        if self.name == "logo.png":
            raise FileNotFoundError(self)
        return real_open(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    run = run_backup()
    assert run.status == "SUCCESS", run.error
    out = tmp_path / "m"
    restore_archive(settings.DNB_BACKUP_DIR / run.filename, tmp_path / "r.sqlite3", out, settings.DNB_BACKUP_KEY)
    assert not (out / "logo.png").exists() and (out / "reconciliation" / "rec-1.pdf").exists()


def test_retention_treats_old_and_new_names_alike(tmp_path):
    today = date(2026, 10, 27)
    names = ["dnb-20200105-011500.sqlite3.enc", "dnb-20200120-011500.tar.enc",
             "dnb-20200201-011500.tar.enc", "dnb-20200202-011500.sqlite3.enc"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    apply_retention(tmp_path, today)
    assert {p.name for p in tmp_path.iterdir()} == {"dnb-20200120-011500.tar.enc", "dnb-20200202-011500.sqlite3.enc"}


def test_drill_passes_on_old_bare_database_backup(file_db_with_entries, tmp_path, settings):
    from backup.service import restore_drill, snapshot as snap

    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    (tmp_path / run.filename).unlink()
    plain = snap(tmp_path / "plain.sqlite3")
    old = "dnb-20200101-010000.sqlite3.enc"
    encrypt_file(plain, tmp_path / old, settings.DNB_BACKUP_KEY)
    run.filename = old
    run.save()
    result = restore_drill()
    assert result.ok, result.message
    out = tmp_path / "old-restored.sqlite3"
    restore_archive(tmp_path / old, out, None, settings.DNB_BACKUP_KEY)
    conn = sqlite3.connect(out)
    assert ledger_fingerprint(conn) == run.ledger_fingerprint
    conn.close()


def test_drill_fails_when_archive_has_no_snapshot(file_db_with_entries, tmp_path, settings):
    from backup.service import restore_drill

    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    bad = _plain_tar(settings, tmp_path, [("media/logo.png", b"x")])
    (tmp_path / run.filename).unlink()
    bad.rename(tmp_path / run.filename)
    result = restore_drill()
    assert not result.ok
    assert result.message == "Restore drill failed: the backup has no database in it."


@pytest.mark.parametrize("name", ["../evil.txt", "/etc/evil.txt", "media/../../evil.txt", "other/file.txt"])
def test_unsafe_member_names_are_rejected(file_db_with_entries, tmp_path, settings, name):
    archive = _plain_tar(settings, tmp_path, [("snapshot.sqlite3", b"x"), (name, b"evil")])
    with pytest.raises(BackupError, match="unsafe"):
        restore_archive(archive, tmp_path / "r.sqlite3", tmp_path / "m", settings.DNB_BACKUP_KEY)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path / "r.sqlite3").exists() and not (tmp_path / "m").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_links_and_devices_are_rejected(file_db_with_entries, tmp_path, settings, kind):
    info = tarfile.TarInfo("media/x")
    info.type, info.linkname = kind, "/etc/passwd"
    archive = _plain_tar(settings, tmp_path, [("snapshot.sqlite3", b"x"), ("media/x", info)])
    with pytest.raises(BackupError, match="unsafe"):
        restore_archive(archive, tmp_path / "r.sqlite3", tmp_path / "m", settings.DNB_BACKUP_KEY)
    with pytest.raises(BackupError, match="unsafe"):
        read_snapshot_bytes(archive, settings.DNB_BACKUP_KEY)


def test_restore_refuses_existing_media_folder_with_files(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path / "b"
    settings.DNB_BACKUP_DIR.mkdir()
    _media(settings, tmp_path)
    run = run_backup()
    out = tmp_path / "m"
    out.mkdir()
    (out / "keep.txt").write_text("keep")
    with pytest.raises(BackupError, match="already exists"):
        restore_archive(settings.DNB_BACKUP_DIR / run.filename, tmp_path / "r.sqlite3", out, settings.DNB_BACKUP_KEY)
    assert (out / "keep.txt").read_text() == "keep" and not (tmp_path / "r.sqlite3").exists()

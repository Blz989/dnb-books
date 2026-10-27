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
from backup.service import (
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


def test_backup_filename():
    assert backup_filename(datetime(2026, 10, 27, 1, 15, 3)) == "dnb-20261027-011503.sqlite3.enc"


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
    decrypt_file(tmp_path / run.filename, restored, settings.DNB_BACKUP_KEY)
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

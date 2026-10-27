import io
import sqlite3
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from backup.models import BackupRun
from backup.service import decrypt_file, encrypt_file, ledger_fingerprint, restore_drill, run_backup


def test_drill_passes_on_fresh_backup(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    result = restore_drill()
    assert result.ok and result.backup_file == run.filename
    assert result.expected == result.actual == run.ledger_fingerprint
    assert "passed" in result.message


def test_drill_detects_tampered_backup(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    target = tmp_path / run.filename
    plain = tmp_path / "plain.sqlite3"
    decrypt_file(target, plain, settings.DNB_BACKUP_KEY)
    conn = sqlite3.connect(plain)
    conn.execute("DROP TRIGGER ledger_line_no_update")  # tamper the copy, not production
    conn.execute("UPDATE ledger_journalline SET debit = 999 WHERE id = (SELECT MIN(id) FROM ledger_journalline)")
    conn.commit()
    conn.close()
    encrypt_file(plain, target, settings.DNB_BACKUP_KEY)
    result = restore_drill()
    assert not result.ok and result.expected != result.actual
    assert "does not match" in result.message


def test_drill_fails_when_file_is_missing(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    run = run_backup()
    (tmp_path / run.filename).unlink()
    result = restore_drill()
    assert not result.ok and run.filename in result.message


def test_drill_fails_with_wrong_key(file_db_with_entries, tmp_path, settings):
    from cryptography.fernet import Fernet

    settings.DNB_BACKUP_DIR = tmp_path
    run_backup()
    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    result = restore_drill()
    assert not result.ok and "key" in result.message.lower()


def test_drill_with_no_backup_yet(db):
    result = restore_drill()
    assert not result.ok and "No successful backup" in result.message


def test_restore_drill_command_exit_codes(file_db_with_entries, tmp_path, settings):
    settings.DNB_BACKUP_DIR = tmp_path
    with pytest.raises(CommandError):
        call_command("restore_drill")  # nothing backed up yet
    run_backup()
    out = io.StringIO()
    call_command("restore_drill", stdout=out)
    assert "passed" in out.getvalue()


def test_restore_refuses_overwrite(tmp_path, settings):
    from cryptography.fernet import Fernet

    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    src, enc, target = tmp_path / "a.bin", tmp_path / "a.enc", tmp_path / "existing.sqlite3"
    src.write_bytes(b"x")
    encrypt_file(src, enc, settings.DNB_BACKUP_KEY)
    target.write_bytes(b"keep me")
    with pytest.raises(CommandError, match="already exists"):
        call_command("restore_backup", str(enc), "--to", str(target))
    assert target.read_bytes() == b"keep me"


def test_restore_backup_writes_readable_database(file_db_with_entries, tmp_path, settings):
    backups = tmp_path / "backups"
    backups.mkdir()
    settings.DNB_BACKUP_DIR = backups
    run = run_backup()
    target = tmp_path / "restored.sqlite3"
    out = io.StringIO()
    call_command("restore_backup", str(backups / run.filename), "--to", str(target), stdout=out)
    conn = sqlite3.connect(target)
    assert ledger_fingerprint(conn) == run.ledger_fingerprint
    conn.close()
    assert "Stop the app" in out.getvalue()


def test_restore_backup_missing_file(tmp_path):
    with pytest.raises(CommandError, match="not found"):
        call_command("restore_backup", str(tmp_path / "nope.enc"), "--to", str(tmp_path / "out.sqlite3"))


def test_restore_backup_wrong_key(tmp_path, settings):
    from cryptography.fernet import Fernet

    src, enc = tmp_path / "a.bin", tmp_path / "a.enc"
    src.write_bytes(b"x")
    encrypt_file(src, enc, Fernet.generate_key().decode())
    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    with pytest.raises(CommandError, match="key"):
        call_command("restore_backup", str(enc), "--to", str(tmp_path / "out.sqlite3"))
    assert not (tmp_path / "out.sqlite3").exists()

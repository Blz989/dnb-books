import hashlib
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

from .models import BackupRun

FILENAME_RE = re.compile(r"^dnb-(\d{8})-(\d{6})\.sqlite3\.enc$")
CENT = Decimal("0.01")
DAILY_DAYS = 30


class BackupError(Exception):
    """A problem with a plain-language message that is shown to the user as is."""


def snapshot(dest: Path) -> Path:
    """Consistent copy using SQLite's online backup API (safe while the app is writing)."""
    src = sqlite3.connect(settings.DNB_DB_PATH)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def _money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT)


def ledger_fingerprint(conn: sqlite3.Connection) -> str:
    """SHA-256 over per-account posted totals, so a restored copy can be compared to production."""
    rows = conn.execute(
        """
        SELECT l.account_id, SUM(l.debit), SUM(l.credit)
        FROM ledger_journalline l JOIN ledger_journalentry e ON e.id = l.entry_id
        WHERE e.status IN ('POSTED', 'VOID')
        GROUP BY l.account_id
        """
    ).fetchall()
    lines = sorted(f"{account_id}|{_money(d)}|{_money(c)}" for account_id, d, c in rows)
    count = conn.execute("SELECT COUNT(*) FROM ledger_journalentry WHERE status IN ('POSTED', 'VOID')").fetchone()[0]
    lines.append(f"entries|{count}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def encrypt_file(src: Path, dst: Path, key: str) -> None:
    Path(dst).write_bytes(Fernet(key.encode()).encrypt(Path(src).read_bytes()))


def decrypt_file(src: Path, dst: Path, key: str) -> None:
    Path(dst).write_bytes(Fernet(key.encode()).decrypt(Path(src).read_bytes()))


def backup_filename(ts: datetime) -> str:
    return f"dnb-{ts:%Y%m%d-%H%M%S}.sqlite3.enc"


def _file_timestamp(path: Path) -> datetime | None:
    m = FILENAME_RE.match(path.name)
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None


def apply_retention(directory: Path, today: date) -> list[Path]:
    """Keep the last 30 days of files plus the newest file of every calendar month."""
    cutoff = today - timedelta(days=DAILY_DAYS - 1)
    dated = [(ts, p) for p in Path(directory).iterdir() if (ts := _file_timestamp(p))]
    newest_per_month = {}
    by_month = defaultdict(list)
    for ts, p in dated:
        by_month[(ts.year, ts.month)].append((ts, p))
    for month, items in by_month.items():
        newest_per_month[month] = max(items, key=lambda i: i[0])[1]
    deleted = []
    for ts, p in dated:
        if ts.date() >= cutoff or newest_per_month[(ts.year, ts.month)] == p:
            continue
        p.unlink()
        deleted.append(p)
    return deleted


def run_backup(now: datetime | None = None) -> BackupRun:
    now = timezone.localtime(now or timezone.now())
    run = BackupRun.objects.create(started_at=now, status=BackupRun.Status.RUNNING)
    dest_dir = Path(settings.DNB_BACKUP_DIR)
    try:
        key = settings.DNB_BACKUP_KEY
        if not key:
            raise BackupError("No backup key set. Run generate_backup_key.")
        try:
            Fernet(key.encode())
        except ValueError:
            raise BackupError("The backup key in .env is not valid. Run generate_backup_key.") from None
        if not dest_dir.is_dir():
            raise BackupError(f"Backup folder {dest_dir} does not exist.")

        name = backup_filename(now.replace(tzinfo=None))
        final = dest_dir / name
        partial = dest_dir / (name + ".part")
        with tempfile.TemporaryDirectory() as tmp:
            snap = snapshot(Path(tmp) / "snapshot.sqlite3")
            conn = sqlite3.connect(snap)
            try:
                fingerprint = ledger_fingerprint(conn)
            finally:
                conn.close()
            try:
                encrypt_file(snap, partial, key)
                os.replace(partial, final)
            except OSError as exc:
                partial.unlink(missing_ok=True)
                raise BackupError(f"Couldn't write to the backup folder {dest_dir}: {exc.strerror or exc}.") from exc
        apply_retention(dest_dir, now.date())

        run.status = BackupRun.Status.SUCCESS
        run.filename = name
        run.size_bytes = final.stat().st_size
        run.ledger_fingerprint = fingerprint
    except BackupError as exc:
        run.status, run.error = BackupRun.Status.FAILED, str(exc)
    except Exception as exc:  # noqa: BLE001 - recorded for the home screen, never raised to the scheduler
        run.status, run.error = BackupRun.Status.FAILED, f"Backup failed: {exc}"
    run.finished_at = timezone.now()
    run.save()
    return run


@dataclass
class DrillResult:
    ok: bool
    backup_file: str
    expected: str
    actual: str
    message: str


def restore_drill() -> DrillResult:
    """Decrypt the newest good backup into a scratch folder and compare its ledger to what was recorded."""
    run = BackupRun.objects.filter(status=BackupRun.Status.SUCCESS).order_by("-started_at", "-id").first()
    if run is None:
        return DrillResult(False, "", "", "", "No successful backup to check yet. Run backup_now first.")
    source = Path(settings.DNB_BACKUP_DIR) / run.filename

    def fail(message, actual=""):
        return DrillResult(False, run.filename, run.ledger_fingerprint, actual, f"Restore drill failed: {message}")

    if not source.exists():
        return fail(f"{run.filename} is missing from {settings.DNB_BACKUP_DIR}.")
    with tempfile.TemporaryDirectory() as tmp:
        restored = Path(tmp) / "restored.sqlite3"
        try:
            decrypt_file(source, restored, settings.DNB_BACKUP_KEY)
        except (InvalidToken, ValueError):
            return fail("couldn't decrypt the backup. The key in .env may not be the one it was made with.")
        try:
            conn = sqlite3.connect(f"{restored.as_uri()}?mode=ro", uri=True)
            try:
                actual = ledger_fingerprint(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            return fail(f"the restored file isn't a readable database ({exc}).")
    if actual != run.ledger_fingerprint:
        return fail(f"the ledger in {run.filename} does not match what was backed up.", actual)
    return DrillResult(
        True, run.filename, run.ledger_fingerprint, actual,
        f"Restore drill passed: {run.filename} restores and its ledger matches.",
    )


@dataclass
class BackupStatus:
    level: str  # "ok" | "warning" | "error"
    message: str


STALE_AFTER = timedelta(hours=26)


def _friendly(ts: datetime) -> str:
    ts = timezone.localtime(ts)
    return f"{ts:%b} {ts.day}, {ts.year} at {ts.hour % 12 or 12}:{ts:%M} {ts:%p}"  # no %-d: works on Windows


def backup_status(now: datetime) -> BackupStatus:
    finished = BackupRun.objects.exclude(status=BackupRun.Status.RUNNING).order_by("-started_at", "-id")
    latest = finished.first()
    if latest is not None and latest.status == BackupRun.Status.FAILED:
        return BackupStatus("error", f"Last backup failed: {latest.error}")
    success = finished.filter(status=BackupRun.Status.SUCCESS).first()
    if success is None or now - (success.finished_at or success.started_at) > STALE_AFTER:
        return BackupStatus("warning", "No successful backup in the last 26 hours.")
    return BackupStatus("ok", f"Last backup {_friendly(success.finished_at or success.started_at)}.")

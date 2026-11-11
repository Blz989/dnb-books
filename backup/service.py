import hashlib
import io
import os
import re
import sqlite3
import tarfile
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

from . import remote
from .models import BackupRun

# .tar.enc is the current format (database and uploaded files); .sqlite3.enc is the old bare database.
FILENAME_RE = re.compile(r"^dnb-(\d{8})-(\d{6})\.(?:tar|sqlite3)\.enc$")
SNAPSHOT_NAME = "snapshot.sqlite3"
MEDIA_DIR = "media"
SQLITE_MAGIC = b"SQLite format 3\x00"
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
    return f"dnb-{ts:%Y%m%d-%H%M%S}.tar.enc"


def build_archive(snap: Path, media_root: Path, dest: Path) -> None:
    """Tar the database snapshot and the media folder. Symlinks are skipped, and a file that
    changes or disappears while we read it is skipped rather than failing the backup."""
    with tarfile.open(dest, "w") as tar:
        tar.add(snap, arcname=SNAPSHOT_NAME, recursive=False)
        folder = tarfile.TarInfo(MEDIA_DIR)
        folder.type, folder.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(folder)
        media_root = Path(media_root)
        if not media_root.is_dir() or media_root.is_symlink():
            return
        for dirpath, dirnames, filenames in os.walk(media_root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
            rel_dir = Path(dirpath).relative_to(media_root)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    data = path.read_bytes()  # size taken from what was read, so a changing file can't corrupt the tar
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                info = tarfile.TarInfo((Path(MEDIA_DIR) / rel_dir / name).as_posix())
                info.size, info.mtime, info.mode = len(data), mtime, 0o644
                tar.addfile(info, io.BytesIO(data))


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """The members of a backup archive, or BackupError if any could escape the folder it is restored into."""
    unsafe = BackupError("the backup contains unsafe file names or links.")
    members = tar.getmembers()
    for m in members:
        parts = m.name.split("/")
        if m.name.startswith("/") or "\\" in m.name or ".." in parts or "" in parts[:-1]:
            raise unsafe
        if not (m.isfile() or m.isdir()):
            raise unsafe
        if m.name != SNAPSHOT_NAME and m.name != MEDIA_DIR and parts[0] != MEDIA_DIR:
            raise unsafe
        if (m.name == SNAPSHOT_NAME and not m.isfile()) or (m.name == MEDIA_DIR and not m.isdir()):
            raise unsafe
    return members


def _decrypt_bytes(source: Path, key: str) -> bytes:
    return Fernet(key.encode()).decrypt(Path(source).read_bytes())


def _open_archive(data: bytes) -> tarfile.TarFile | None:
    """The tar inside a decrypted backup, or None for an old backup (a bare database)."""
    if data.startswith(SQLITE_MAGIC):
        return None
    try:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError:
        raise BackupError("the backup isn't a readable archive.") from None


def read_snapshot_bytes(source: Path, key: str) -> bytes:
    """The database inside a backup of either format. Nothing is written to disk."""
    data = _decrypt_bytes(source, key)
    tar = _open_archive(data)
    if tar is None:
        return data
    with tar:
        try:
            members = _safe_members(tar)
        except tarfile.TarError:
            raise BackupError("the backup isn't a readable archive.") from None
        if not any(m.name == SNAPSHOT_NAME for m in members):
            raise BackupError("the backup has no database in it.")
        return tar.extractfile(SNAPSHOT_NAME).read()


def restore_archive(source: Path, db_dest: Path, media_dest: Path | None, key: str) -> bool:
    """Write the database to db_dest and, for the current format, the uploaded files into media_dest.

    Never overwrites: db_dest must not exist, media_dest must not exist or be empty. Every name is
    checked before anything is written, and files are written by reading each member, never by tar extraction.
    Returns True when the backup held uploaded files (False for an old bare-database backup)."""
    db_dest = Path(db_dest)
    if db_dest.exists():
        raise BackupError(f"{db_dest} already exists. Choose a new file name; nothing was overwritten.")
    if media_dest is not None:
        media_dest = Path(media_dest)
        if media_dest.exists() and (not media_dest.is_dir() or any(media_dest.iterdir())):
            raise BackupError(f"{media_dest} already exists. Choose a new folder name; nothing was overwritten.")
    data = _decrypt_bytes(source, key)
    tar = _open_archive(data)
    if tar is None:
        db_dest.write_bytes(data)
        return False
    with tar:
        try:
            members = _safe_members(tar)
        except tarfile.TarError:
            raise BackupError("the backup isn't a readable archive.") from None
        if not any(m.name == SNAPSHOT_NAME for m in members):
            raise BackupError("the backup has no database in it.")
        db_dest.write_bytes(tar.extractfile(SNAPSHOT_NAME).read())
        if media_dest is not None:
            media_dest.mkdir(parents=True, exist_ok=True)
            base = media_dest.resolve()
            for m in members:
                if m.name == SNAPSHOT_NAME or m.name == MEDIA_DIR:
                    continue
                target = (media_dest / Path(m.name).relative_to(MEDIA_DIR)).resolve()
                if base not in target.parents and target != base:
                    raise BackupError("the backup contains unsafe file names or links.")
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(m).read())
    return True


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
            snap = snapshot(Path(tmp) / SNAPSHOT_NAME)
            conn = sqlite3.connect(snap)
            try:
                fingerprint = ledger_fingerprint(conn)
            finally:
                conn.close()
            try:
                archive = Path(tmp) / "backup.tar"
                build_archive(snap, Path(settings.MEDIA_ROOT), archive)
                encrypt_file(archive, partial, key)
                os.replace(partial, final)
            except OSError as exc:
                partial.unlink(missing_ok=True)
                raise BackupError(f"Couldn't write to the backup folder {dest_dir}: {exc.strerror or exc}.") from exc
        deleted = apply_retention(dest_dir, now.date())
        run.filename = name
        if remote.enabled():
            try:
                remote.upload(final)
                remote.delete([p.name for p in deleted])
            except remote.RemoteError as exc:
                raise BackupError(f"Backup saved locally but not uploaded: {exc}") from exc

        run.status = BackupRun.Status.SUCCESS
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

    if not remote.enabled() and not source.exists():
        return fail(f"{run.filename} is missing from {settings.DNB_BACKUP_DIR}.")
    with tempfile.TemporaryDirectory() as tmp:
        if remote.enabled():
            # Check the off-site copy, the one that matters if this server is lost.
            source = Path(tmp) / run.filename
            try:
                remote.download(run.filename, source)
            except remote.RemoteError as exc:
                return fail(f"couldn't download {run.filename} from {settings.DNB_BACKUP_REMOTE}: {exc}")
        restored = Path(tmp) / "restored.sqlite3"
        try:
            restored.write_bytes(read_snapshot_bytes(source, settings.DNB_BACKUP_KEY))
        except (InvalidToken, ValueError):
            return fail("couldn't decrypt the backup. The key in .env may not be the one it was made with.")
        except BackupError as exc:
            return fail(str(exc))
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

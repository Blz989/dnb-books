"""import_zoho: replay a Zoho export into a copy of the database, tie it out, and swap the copy in.

The parent process checks the database and the export, snapshots the database to
`{DNB_DB_PATH}.import-copy`, and starts a child process (`--_into-copy`) with DNB_DB_PATH pointing
at the copy, so the parent's own connection never touches it. The real database file is only
replaced, after a backup, when the child finished and the tie-out passed (or the owner accepted the
differences). Anything else deletes the copy and leaves the real file as it was.
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connections

from core.roles import is_admin
from migration.zoho.reader import ExportError, read_export
from migration.zoho.validate import validate

NON_EMPTY = "The database already has documents. Import only into a new, empty database."
COPY_SUFFIX = ".import-copy"
SIDE_FILES = ("-wal", "-shm", "-journal")

# Child exit codes. Anything else (an uncaught error exits 1) means the replay failed.
PASSED, DIFFERENCES, FAILED, UNDEPOSITED = 0, 4, 3, 5


def _remove_db_file(path: Path) -> None:
    for p in (path, *(Path(f"{path}{s}") for s in SIDE_FILES)):
        p.unlink(missing_ok=True)


class Command(BaseCommand):
    help = "Import a Zoho Books export into a new, empty database (always through a copy)."

    def add_arguments(self, parser):
        parser.add_argument("export", help="The Zoho export: the downloaded .zip or its unzipped folder")
        parser.add_argument("--reports", required=True, help="Folder with Zoho's reports for the tie-out")
        parser.add_argument("--user", required=True, help="Admin username the import posts as")
        parser.add_argument("--dry-run", action="store_true", help="Write the report and keep nothing else")
        parser.add_argument("--opening-balances", help="opening_balances.csv (account,debit,credit)")
        parser.add_argument("--deposit-pairs", help="deposit_pairs.csv (date,amount,payment_numbers)")
        parser.add_argument("--keep-going", action="store_true",
                            help="Dry run only: skip a document that fails, list it, and carry on")
        parser.add_argument("--accept-differences", action="store_true",
                            help="Swap the import in even though the tie-out did not pass")
        parser.add_argument("--_into-copy", dest="into_copy", action="store_true", help=argparse.SUPPRESS)

    def handle(self, *args, **opts):
        if opts["into_copy"]:
            from migration.import_child import run_child

            code = run_child(self, opts)
        else:
            code = self._parent(opts)
        if code:
            raise SystemExit(code)

    # ---- parent ----
    def _fail(self, code: int, *lines: str) -> int:
        for line in lines:
            self.stderr.write(line)
        return code

    def _parent(self, opts) -> int:
        from ledger.models import JournalEntry
        from purchasing.models import Bill, VendorPayment
        from sales.models import Invoice, Payment

        if any(m.objects.exists() for m in (Invoice, Bill, Payment, VendorPayment, JournalEntry)):
            return self._fail(2, NON_EMPTY)
        if opts["keep_going"] and not opts["dry_run"]:
            return self._fail(2, "--keep-going only works with --dry-run.")
        user = get_user_model().objects.filter(username=opts["user"]).first()
        if user is None:
            return self._fail(2, f'There is no user named "{opts["user"]}".')
        if not is_admin(user):
            return self._fail(2, f"{user.username} is not an admin. Import as an admin user.")
        export = Path(opts["export"]).resolve()
        if not export.exists():
            return self._fail(2, f"{export} does not exist.")
        for option in ("opening_balances", "deposit_pairs"):
            if opts[option] and not Path(opts[option]).is_file():
                return self._fail(2, f"{opts[option]} does not exist.")
        try:
            problems = validate(read_export(export))
        except ExportError as exc:
            problems = exc.problems
        if problems:
            return self._fail(2, *problems)

        from backup.service import run_backup, snapshot

        db = Path(settings.DNB_DB_PATH)
        copy = Path(f"{db}{COPY_SUFFIX}")
        _remove_db_file(copy)  # left over from a run that was killed
        try:
            snapshot(copy)
            code = self._run_child(copy, export, opts)
        except BaseException:
            self._discard(copy)
            raise
        if code not in (PASSED, DIFFERENCES):
            self._discard(copy)
            return self._fail(1, "Import stopped. The database was not changed.")
        if opts["dry_run"]:
            self._discard(copy)
            self.stdout.write("Dry run finished. The database was not changed.")
            return 0 if code == PASSED else 1
        if code == DIFFERENCES and not opts["accept_differences"]:
            self._discard(copy)
            return self._fail(
                1, "The tie-out did not pass, so nothing was imported. Read the report and fix the differences, "
                   "or run again with --accept-differences to import anyway.")

        backup = run_backup()
        if backup.status != "SUCCESS":
            self._discard(copy)
            return self._fail(1, f"{backup.error} Nothing was imported.")
        self._swap(copy, db)
        self.stdout.write("Imported. Restart the app.")
        return 0

    def _run_child(self, copy: Path, export: Path, opts) -> int:
        cmd = [
            sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "import_zoho", str(export),
            "--_into-copy", "--reports", str(Path(opts["reports"]).resolve()), "--user", opts["user"],
        ]
        for flag in ("dry_run", "accept_differences", "keep_going"):
            if opts[flag]:
                cmd.append("--" + flag.replace("_", "-"))
        for option in ("opening_balances", "deposit_pairs"):
            if opts[option]:
                cmd += ["--" + option.replace("_", "-"), str(Path(opts[option]).resolve())]
        env = {
            **os.environ, "DNB_DB_PATH": str(copy), "DNB_SECRET_KEY": settings.SECRET_KEY,
            "PYTHONUNBUFFERED": "1",
        }
        child = subprocess.Popen(
            cmd, cwd=settings.BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        errors: list[str] = []
        reader = threading.Thread(target=lambda: errors.extend(child.stderr), daemon=True)
        reader.start()
        for line in child.stdout:
            self.stdout.write(line.rstrip("\n"))
        code = child.wait()
        reader.join()
        for line in errors:
            self.stderr.write(line.rstrip("\n"))
        return code

    def _discard(self, copy: Path) -> None:
        """Deletes the copy, its -wal/-shm files, and any report PDF the child saved for it."""
        media = Path(settings.MEDIA_ROOT)
        if copy.exists():
            try:
                conn = sqlite3.connect(copy)
                try:
                    names = [r[0] for r in conn.execute("SELECT report_pdf FROM migration_importrun") if r[0]]
                finally:
                    conn.close()
            except sqlite3.Error:
                names = []
            for name in names:
                (media / name).unlink(missing_ok=True)
        _remove_db_file(copy)

    def _swap(self, copy: Path, db: Path) -> None:
        connections.close_all()
        # Fold the copy's WAL into the file itself, so the one file we move is complete.
        conn = sqlite3.connect(copy)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()
        for suffix in SIDE_FILES:
            Path(f"{copy}{suffix}").unlink(missing_ok=True)
            # A stale -wal next to the new file would be replayed into it on the next open.
            Path(f"{db}{suffix}").unlink(missing_ok=True)
        os.replace(copy, db)

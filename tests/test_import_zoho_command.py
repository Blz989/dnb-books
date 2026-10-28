"""import_zoho: the real database is only ever replaced by a finished copy (Review Focus 2 and 3).

These tests drive the command against the file-backed test database. The replay itself runs in a
child process against a copy, so they take a few seconds each.
"""
import io
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import connection, connections, transaction

from backup.service import snapshot
from ledger.models import JournalEntry
from migration.models import ImportRun
from migration.zoho import mapping
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from purchasing.models import Bill, VendorPayment
from sales.models import Invoice, Payment
from tests.test_zoho_tieout import make_reports
from tests.zoho_sample import CUSTOMERS, build_sample_export

D = Decimal
NON_EMPTY = "The database already has documents. Import only into a new, empty database."


class _Rollback(Exception):
    pass


@pytest.fixture
def empty_file_db(transactional_db, items_purge, ledger_purge, settings, tmp_path, admin_user):
    """The file-backed test database, empty apart from users, with a backup folder and key."""
    settings.DNB_DB_PATH = Path(connection.settings_dict["NAME"])
    settings.DNB_BACKUP_DIR = tmp_path / "backups"
    settings.DNB_BACKUP_DIR.mkdir()
    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    settings.DNB_BACKUP_REMOTE = ""
    pristine = tmp_path / "pristine.sqlite3"
    snapshot(pristine)
    yield settings.DNB_DB_PATH
    # A real run leaves its report PDF in MEDIA_ROOT; don't leave it in data/.
    for run in ImportRun.objects.exclude(report_pdf=""):
        Path(run.report_pdf.path).unlink(missing_ok=True)
    # A real run swapped in a database full of posted documents, which the end-of-test flush
    # cannot delete. Put the empty database back (same page-for-page copy, triggers included).
    connections.close_all()
    src, dst = sqlite3.connect(pristine), sqlite3.connect(settings.DNB_DB_PATH)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _export(tmp_path, **kwargs):
    return build_sample_export(tmp_path / "zoho", **kwargs)


def _matching_reports(tmp_path, user):
    """Zoho reports that agree with the sample: replay it here, write the reports, roll it all back."""
    folder = _export(tmp_path / "scratch")
    reports = tmp_path / "reports"
    try:
        with transaction.atomic():
            export = read_export(folder)
            replay = Replay(export, user)
            replay.load_master()
            for event in replay.events():
                replay.post(event)
            replay.finish()
            make_reports(reports, mapping.account_plan(export.files["accounts"]))
            raise _Rollback
    except _Rollback:
        pass
    assert not JournalEntry.objects.exists()
    return reports


def _run(*args):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("import_zoho", *[str(a) for a in args], stdout=out, stderr=err)
    except SystemExit as exc:
        code = exc.code
    connections.close_all()  # the command may have swapped the file under this connection
    return code, out.getvalue(), err.getvalue()


def _no_documents():
    return not any(m.objects.exists() for m in (Invoice, Bill, Payment, VendorPayment, JournalEntry))


def _leftovers(db_path):
    return [p for p in Path(db_path).parent.iterdir() if p.name.startswith(Path(db_path).name + ".import-copy")]


def _fingerprint(db_path):
    """Row count of every table: the import adds rows to almost all of them."""
    conn = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        conn.close()


def test_refuses_non_empty_database(file_db_with_entries, tmp_path):
    code, out, err = _run(_export(tmp_path), "--reports", tmp_path, "--user", "admin")
    assert code == 2
    assert NON_EMPTY in err
    assert JournalEntry.objects.count() == 3


def test_validation_problems_exit_2(empty_file_db, tmp_path):
    bad = {"Invoice ID": "X1", "Invoice Number": "INV-X1", "Invoice Status": "Open",
           "Customer Name": "Nobody Inc", "Invoice Date": "2024-05-01", "Total": "10.00",
           "Item Total": "10.00", "Payment Terms Label": "Net 30", "Item Name": "Blue Widget",
           "SKU": "WID-A", "Quantity": "1", "Item Price": "10.00"}
    code, out, err = _run(_export(tmp_path, extra_rows={"invoices": [bad]}), "--reports", tmp_path,
                          "--user", "admin", "--dry-run")
    assert code == 2
    lines = err.strip().splitlines()
    assert any(l.endswith('customer "Nobody Inc" is not in the export.') for l in lines)
    assert _no_documents() and not _leftovers(empty_file_db)


def test_dry_run_leaves_database_untouched(empty_file_db, tmp_path):
    export = _export(tmp_path)
    before = _fingerprint(empty_file_db)
    code, out, err = _run(export, "--reports", tmp_path / "none", "--user", "admin", "--dry-run")
    assert code == 1, out + err  # no Zoho reports given, so the tie-out fails
    assert "2024-01: " in out and " events" in out
    assert (export.parent / "report.html").exists()
    assert (export.parent / "report.pdf").read_bytes().startswith(b"%PDF")
    assert _no_documents() and not ImportRun.objects.exists()
    assert not _leftovers(empty_file_db)
    assert _fingerprint(empty_file_db) == before


def test_failure_midway_leaves_database_untouched(empty_file_db, tmp_path):
    unbalanced = [
        {"Journal ID": "59000000009", "Journal Date": "2025-12-31", "Journal Number": "9",
         "Notes": "Broken", "Account": "Office Supplies", "Debit": "10.00", "Credit": "0.00",
         "Description": ""},
        {"Journal ID": "59000000009", "Journal Date": "2025-12-31", "Journal Number": "9",
         "Notes": "Broken", "Account": "Bank Fees and Charges", "Debit": "0.00", "Credit": "5.00",
         "Description": ""},
    ]
    export = _export(tmp_path, extra_rows={"journals": unbalanced})
    before = _fingerprint(empty_file_db)
    code, out, err = _run(export, "--reports", tmp_path / "none", "--user", "admin", "--accept-differences")
    assert code == 1
    assert "Journal.csv" in err and "59000000009" in err
    assert "2024-01: " in out  # it got partway
    assert _no_documents() and not ImportRun.objects.exists()
    assert not _leftovers(empty_file_db)
    assert not list(Path(empty_file_db).parent.glob("*.import-copy*"))
    assert _fingerprint(empty_file_db) == before


def test_real_run_swaps_in_copy_after_backup(empty_file_db, tmp_path, settings, admin_user):
    reports = _matching_reports(tmp_path, admin_user)
    code, out, err = _run(_export(tmp_path), "--reports", reports, "--user", "admin")
    assert code == 0, out + err
    assert out.strip().endswith("Imported. Restart the app.")
    assert Invoice.objects.exists() and Bill.objects.exists() and Payment.objects.exists()
    assert list(Path(settings.DNB_BACKUP_DIR).glob("dnb-*.sqlite3.enc"))
    run = ImportRun.objects.get()
    assert run.succeeded and not run.dry_run and run.user.username == "admin"
    assert run.report_pdf and run.summary["tieout_passed"] is True
    assert not _leftovers(empty_file_db)


def test_real_run_refuses_failed_tieout_without_accept(empty_file_db, tmp_path, settings):
    before = _fingerprint(empty_file_db)
    code, out, err = _run(_export(tmp_path), "--reports", tmp_path / "none", "--user", "admin")
    assert code == 1
    assert "--accept-differences" in err
    assert _no_documents() and not ImportRun.objects.exists()
    assert not list(Path(settings.DNB_BACKUP_DIR).iterdir())
    assert not _leftovers(empty_file_db)
    assert _fingerprint(empty_file_db) == before


def test_real_run_with_accept_differences_imports(empty_file_db, tmp_path):
    code, out, err = _run(_export(tmp_path), "--reports", tmp_path / "none", "--user", "admin",
                          "--accept-differences")
    assert code == 0, out + err
    assert Invoice.objects.exists()
    run = ImportRun.objects.get()
    assert run.summary["tieout_passed"] is False and run.summary["accepted_differences"] is True


def test_undeposited_transfer_warning_fails_the_run(empty_file_db, tmp_path):
    late = {"Transaction Date": "2025-06-30", "Transaction Type": "Funds Transfer",
            "From Account": "Undeposited Funds", "To Account": "BUS COMPLETE CHK", "Reference": "DEP-9",
            "Description": "Deposit", "Total": "999.99"}
    export = _export(tmp_path, extra_rows={"transfers": [late]})
    code, out, err = _run(export, "--reports", tmp_path / "none", "--user", "admin", "--accept-differences")
    assert code == 1
    assert "Undeposited transfer 2025-06-30 999.99: no exact set of payments." in out + err
    assert (export.parent / "report.html").exists()
    assert _no_documents() and not _leftovers(empty_file_db)


def test_numbers_after_real_run(empty_file_db, tmp_path, admin_user):
    from contacts.models import Customer
    from items.models import Item
    from sales.models import DocType
    from sales.numbering import next_number
    from sales.posting.invoices import InvoiceLineSpec, create_invoice

    reports = _matching_reports(tmp_path, admin_user)
    code, out, err = _run(_export(tmp_path), "--reports", reports, "--user", "admin")
    assert code == 0, out + err
    # Zoho's payment numbers come over exactly, in their mixed formats.
    assert {"1", "CP-1002"} <= set(Payment.objects.values_list("number", flat=True))
    with transaction.atomic():
        assert next_number(DocType.PMT) == "PMT-00001"
    highest = max(int(n[4:]) for n in Invoice.objects.filter(number__startswith="INV-").values_list("number", flat=True))
    invoice = create_invoice(
        customer=Customer.objects.get(name=CUSTOMERS[0]), date=date(2026, 10, 1), user=admin_user,
        lines=[InvoiceLineSpec(Item.objects.get(sku="SHIP"), 1, D("5.00"))])
    assert invoice.number == f"INV-{highest + 1:06d}"

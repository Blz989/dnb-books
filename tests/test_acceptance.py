"""Spec section 12, "Phase 1 done when", driven only through the web UI and management commands."""
import io
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import connection
from django.urls import reverse

from ledger.models import Account, EntryStatus, JournalEntry


@pytest.fixture
def acceptance_env(transactional_db, ledger_purge, settings, tmp_path, admin_user, client):
    settings.DNB_DB_PATH = Path(connection.settings_dict["NAME"])
    settings.DNB_BACKUP_DIR = tmp_path
    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    client.force_login(admin_user)
    return client


def test_phase_1_acceptance(acceptance_env):
    client = acceptance_env

    # 1. Two accounts, created in admin.
    for number, name, type_, subtype in (
        ("1000", "Checking", "ASSET", "BANK"),
        ("4000", "Sales", "INCOME", "INCOME"),
    ):
        r = client.post(reverse("admin:ledger_account_add"), {
            "number": number, "name": name, "type": type_, "subtype": subtype, "is_active": "on", "description": "",
        })
        assert r.status_code == 302, r.content.decode()[:500]
    cash, sales = Account.objects.get(number="1000"), Account.objects.get(number="4000")

    # 2. A draft entry with two lines.
    r = client.post(reverse("admin:ledger_journalentry_add"), {
        "date": "2026-01-15", "reference": "ACC-1", "memo": "Acceptance sale",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-account": cash.pk, "lines-0-debit": "1234.56", "lines-0-credit": "0", "lines-0-description": "",
        "lines-1-account": sales.pk, "lines-1-debit": "0", "lines-1-credit": "1234.56", "lines-1-description": "",
    })
    assert r.status_code == 302, r.content.decode()[:500]
    entry = JournalEntry.objects.get()
    assert entry.status == EntryStatus.DRAFT

    # 3. Post it with the admin action.
    client.post(reverse("admin:ledger_journalentry_changelist"),
                {"action": "post_drafts", "_selected_action": [entry.pk]}, follow=True)
    entry.refresh_from_db()
    assert entry.status == EntryStatus.POSTED

    # 4. The amount shows on all four reports.
    pages = [
        "/reports/trial-balance/?as_of=2026-01-31",
        "/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31",
        "/reports/balance-sheet/?as_of=2026-01-31",
        f"/reports/general-ledger/?account={cash.pk}&start=2026-01-01&end=2026-01-31",
    ]
    for url in pages:
        r = client.get(url)
        assert r.status_code == 200 and b"1,234.56" in r.content, url

    # 5. Backup runs and the restore drill passes.
    out = io.StringIO()
    call_command("backup_now", stdout=out)
    call_command("restore_drill", stdout=out)
    assert "Backup finished" in out.getvalue() and "Restore drill passed" in out.getvalue()
    assert b"Last backup" in client.get("/").content

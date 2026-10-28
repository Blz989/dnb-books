from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking import inbox
from banking.importers import ParsedTxn, import_transactions
from banking.models import BankTransaction, TxnSource, TxnStatus
from banking.plaid_client import PlaidClient
from tests.fake_plaid import FakePlaidClient

D = Decimal
REASON = "Before the feed start date."


def row(ext, day, amount="-10.00", desc="COFFEE"):
    return ParsedTxn(ext, day, desc, D(amount))


def test_default_feed_start_is_jan_1_2026(checking):
    assert checking.feed_starts == date(2026, 1, 1)


def test_rows_before_feed_start_excluded_with_reason(checking, admin_user):
    result = import_transactions(checking, [
        row("old", date(2025, 12, 31)), row("edge", date(2026, 1, 1)), row("new", date(2026, 2, 3)),
    ], source=TxnSource.FILE, user=admin_user)
    assert (result.added, result.before_feed_start, result.possible_duplicates, result.skipped) == (2, 1, 0, 0)
    old = BankTransaction.objects.get(external_id="old")
    assert (old.status, old.exclude_reason) == (TxnStatus.EXCLUDED, REASON)
    assert BankTransaction.objects.get(external_id="edge").status == TxnStatus.NEW
    # Re-importing the same file skips the set-aside row like any other.
    again = import_transactions(checking, [row("old", date(2025, 12, 31))], source=TxnSource.FILE, user=admin_user)
    assert (again.skipped, again.before_feed_start) == (1, 0)


def test_feed_start_beats_duplicate_reason(checking, admin_user):  # Review Focus 4
    day = date(2025, 11, 20)
    BankTransaction.objects.create(
        bank_account=checking, external_id="plaid-1", date=day, description="COFFEE", amount=D("-10.00"),
        source=TxnSource.PLAID)
    result = import_transactions(checking, [row("file-1", day)], source=TxnSource.FILE, user=admin_user)
    assert (result.before_feed_start, result.possible_duplicates, result.added) == (1, 0, 0)
    t = BankTransaction.objects.get(external_id="file-1")
    assert (t.status, t.exclude_reason) == (TxnStatus.EXCLUDED, REASON)


def test_restore_feed_excluded_row(checking, admin_user):
    import_transactions(checking, [row("old", date(2025, 6, 1))], source=TxnSource.FILE, user=admin_user)
    txn = BankTransaction.objects.get(external_id="old")
    inbox.restore(txn, user=admin_user)
    txn.refresh_from_db()
    assert (txn.status, txn.exclude_reason) == (TxnStatus.NEW, "")


def test_changed_feed_start_applies_to_later_imports(checking, admin_user):
    checking.feed_starts = date(2025, 1, 1)
    checking.save()
    result = import_transactions(checking, [row("x", date(2025, 6, 1))], source=TxnSource.FILE, user=admin_user)
    assert (result.added, result.before_feed_start) == (1, 0)


def test_link_token_requests_365_days(monkeypatch):
    sent = []

    class Api:
        def link_token_create(self, request):
            sent.append(request)

            class Resp:
                def to_dict(self):
                    return {"link_token": "link-x"}
            return Resp()

    client = PlaidClient.__new__(PlaidClient)
    client._api, client._secrets = Api(), []
    assert client.create_link_token("1") == "link-x"
    assert sent[0].transactions.days_requested == 365
    client.create_link_token("1", access_token="access-tok")
    assert "transactions" not in sent[1].to_dict()  # update mode sends no transactions

    fake = FakePlaidClient()
    fake.create_link_token("1")
    fake.create_link_token("1", "access-tok")
    assert fake.link_days_requested == [365, None]


def test_bank_account_form_feed_starts(admin_user, checking):
    c = Client()
    c.force_login(admin_user)
    page = c.get(f"/banking/{checking.pk}/edit/").content.decode()
    assert "Feed starts" in page and "Bank rows dated before this are set aside." in page
    assert 'value="2026-01-01"' in page
    data = {"kind": checking.kind, "mask": "1234", "next_check_number": "2051", "feed_starts": "2025-07-01",
            "is_active": "on"}
    r = c.post(f"/banking/{checking.pk}/edit/", data)
    assert r.status_code == 302
    checking.refresh_from_db()
    assert checking.feed_starts == date(2025, 7, 1)

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from banking import plaid_client
from banking.importers import parse_ofx_balance
from banking.models import PlaidItem
from banking.plaid_client import parse_sync_response
from banking.sync import sync_item
from tests.fake_plaid import FakePlaidClient, load

D = Decimal
BANK = Path(__file__).parent / "fixtures" / "bank"


@pytest.fixture
def item(db, field_key, checking, card_account, monkeypatch):
    it = PlaidItem(institution_name="First Platypus Bank")
    it.set_access_token("access-sandbox-fake-token")
    it.save()
    checking.plaid_item, checking.plaid_account_id = it, "acct-chk"
    checking.save()
    card_account.plaid_item, card_account.plaid_account_id = it, "acct-card"
    card_account.save()
    return it


def use(monkeypatch, client):
    monkeypatch.setattr(plaid_client, "get_client", lambda: client)


def test_sync_page_reads_balances():
    page = parse_sync_response(load("sync_balances.json"))
    assert page.balances == {"acct-chk": D("2450.17"), "acct-card": D("312.55")}
    assert parse_sync_response(load("sync_page1.json")).balances == {}


def test_sync_stores_the_bank_balance(item, monkeypatch, checking, card_account):
    use(monkeypatch, FakePlaidClient(pages=("sync_balances.json",)))
    sync_item(item)
    checking.refresh_from_db()
    card_account.refresh_from_db()
    assert (checking.bank_balance, checking.bank_balance_on) == (D("2450.17"), timezone.localdate())
    assert card_account.bank_balance == D("312.55")  # what is owed, positive


def test_sync_without_balances_keeps_the_last_one(item, monkeypatch, checking):
    checking.bank_balance, checking.bank_balance_on = D("10.00"), date(2026, 1, 2)
    checking.save()
    use(monkeypatch, FakePlaidClient())
    sync_item(item)
    checking.refresh_from_db()
    assert (checking.bank_balance, checking.bank_balance_on) == (D("10.00"), date(2026, 1, 2))


def test_parse_ofx_balance():
    assert parse_ofx_balance((BANK / "checking.ofx").read_bytes()) == (D("1065.00"), date(2026, 1, 31))
    assert parse_ofx_balance(b"not ofx") is None


def _upload(client, account, name):
    data = (BANK / name).read_bytes()
    return client.post(reverse("banking:import-file", args=[account.pk]),
                       {"file": SimpleUploadedFile(name, data)})


def test_ofx_import_stores_balance(client, admin_user, checking, card_account):
    client.force_login(admin_user)
    _upload(client, checking, "checking.ofx")
    _upload(client, card_account, "card.qfx")
    checking.refresh_from_db()
    card_account.refresh_from_db()
    assert (checking.bank_balance, checking.bank_balance_on) == (D("1065.00"), date(2026, 1, 31))
    assert (card_account.bank_balance, card_account.bank_balance_on) == (D("186.40"), date(2026, 1, 31))


def test_csv_import_leaves_balance_alone(client, admin_user, checking):
    checking.bank_balance, checking.bank_balance_on = D("10.00"), date(2026, 1, 2)
    checking.csv_mapping = {"date": "Date", "description": "Description", "amount": "Amount", "date_format": "%m/%d/%Y"}
    checking.save()
    client.force_login(admin_user)
    _upload(client, checking, "checking.csv")
    checking.refresh_from_db()
    assert checking.bank_balance == D("10.00")

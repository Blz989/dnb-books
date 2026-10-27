import html
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from banking import plaid_client
from banking.models import (
    AccountKind, BankAccount, BankTransaction, ItemStatus, PlaidItem, Reconciliation, ReconStatus, TxnSource,
    TxnStatus,
)
from ledger.models import Account, AccountSubtype, AccountType
from ledger.posting import LineSpec, post_entry
from tests.fake_plaid import FakePlaidClient

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "bank"
TOKEN = "access-sandbox-fake-token"


def text(response):
    return html.unescape(response.content.decode())


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def plaid_on(settings):
    settings.PLAID_CLIENT_ID, settings.PLAID_SECRET, settings.PLAID_ENV = "cid", "sec", "sandbox"


@pytest.fixture
def fake(monkeypatch):
    client = FakePlaidClient()
    monkeypatch.setattr(plaid_client, "get_client", lambda: client)
    return client


NEXT_MONTH = b"Date,Description,Amount\n02/03/2026,NEW ONE,5.00\n02/04/2026,NEW TWO,6.00\n02/05/2026,NEW THREE,7.00\n"


def upload(name, data, **extra):
    return {"file": SimpleUploadedFile(name, data), **extra}


# ---- account list ----

def test_pages_need_login(client, checking):
    for path in ("/banking/", "/banking/new/", f"/banking/{checking.pk}/import/", "/banking/link/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


def test_account_list_shows_balance_kind_status_and_inbox_count(adm, checking, card_account, admin_user, bank, card, equity):
    post_entry(date=date(2026, 1, 2), user=admin_user, lines=[
        LineSpec(bank, debit=D("1500.00")), LineSpec(equity, credit=D("1500.00"))])
    post_entry(date=date(2026, 1, 3), user=admin_user, lines=[
        LineSpec(equity, debit=D("200.00")), LineSpec(card, credit=D("200.00"))])
    for i in range(2):
        BankTransaction.objects.create(bank_account=checking, external_id=f"n{i}", date=date(2026, 1, 5),
                                       description="x", amount=D("1.00"), source=TxnSource.FILE)
    BankTransaction.objects.create(bank_account=checking, external_id="done", date=date(2026, 1, 5), description="x",
                                   amount=D("1.00"), source=TxnSource.FILE, status=TxnStatus.EXCLUDED)
    Reconciliation.objects.create(bank_account=checking, statement_date=date(2026, 1, 31), statement_balance=D("0"),
                                  status=ReconStatus.FINISHED, started_by=admin_user)
    page = text(adm.get("/banking/"))
    assert "1,500.00" in page and "Checking" in page and "···· 1234" in page
    assert "Company Visa" in page and "200.00" in page and "owed" in page
    assert "File import" in page and "2026-01-31" in page
    assert "/banking/inbox/?account=%d" % checking.pk in page
    row = [r for r in page.split("<tr") if "1234" in r][0]
    assert ">2<" in row  # two new items


def test_account_list_shows_plaid_status(adm, checking, field_key):
    item = PlaidItem(institution_name="First Platypus Bank", status=ItemStatus.NEEDS_LOGIN)
    item.set_access_token(TOKEN)
    item.save()
    checking.plaid_item = item
    checking.save()
    page = text(adm.get("/banking/"))
    assert "First Platypus Bank" in page and "Needs login" in page and "Re-link" in page
    assert TOKEN not in page and item.access_token not in page


def test_standard_user_sees_list_but_no_admin_buttons(std, checking):
    page = text(std.get("/banking/"))
    assert "Checking" in page and "Link with Plaid" not in page and "Sync now" not in page


def test_admin_adds_a_bank_account_and_standard_cannot(adm, std, bank):
    assert std.get("/banking/new/").status_code == 403
    r = adm.post("/banking/new/", {"ledger_account": bank.pk, "kind": "CHECKING", "mask": "4321",
                                   "next_check_number": "3001", "is_active": "on"})
    assert r.status_code == 302
    acct = BankAccount.objects.get()
    assert (acct.ledger_account, acct.kind, acct.mask, acct.next_check_number) == (bank, "CHECKING", "4321", 3001)


def test_kind_must_fit_the_ledger_account(adm, bank):
    r = adm.post("/banking/new/", {"ledger_account": bank.pk, "kind": "CARD", "mask": "", "next_check_number": "1001"})
    assert r.status_code == 200 and "doesn't fit the ledger account" in text(r)
    assert BankAccount.objects.count() == 0


def test_admin_edits_next_check_number(adm, checking):
    r = adm.post(f"/banking/{checking.pk}/edit/", {
        "ledger_account": checking.ledger_account_id, "kind": "CHECKING", "mask": "1234",
        "next_check_number": "3100", "is_active": "on"})
    assert r.status_code == 302
    checking.refresh_from_db()
    assert checking.next_check_number == 3100


# ---- Plaid link ----

def test_link_is_admin_only(std, adm, plaid_on):
    assert std.get("/banking/link/").status_code == 403
    assert adm.get("/banking/link/").status_code == 200


def test_link_page_hidden_without_plaid_settings(adm, settings):
    settings.PLAID_CLIENT_ID = settings.PLAID_SECRET = ""
    page = text(adm.get("/banking/link/"))
    assert "Plaid isn't set up. Add PLAID_CLIENT_ID and PLAID_SECRET to .env." in page
    assert "cdn.plaid.com" not in page and 'id="link-button"' not in page
    assert "Link with Plaid" not in text(adm.get("/banking/"))


def test_link_page_loads_plaid_javascript_only_here(adm, plaid_on, checking):
    page = text(adm.get("/banking/link/"))
    assert "https://cdn.plaid.com/link/v2/stable/link-initialize.js" in page and 'id="link-button"' in page
    assert "cdn.plaid.com" not in text(adm.get("/banking/"))
    assert "Link with Plaid" in text(adm.get("/banking/"))


def test_link_token_endpoint_returns_a_token(adm, std, plaid_on, fake):
    r = adm.post("/banking/link/token/")
    assert r.status_code == 200 and r.json() == {"link_token": "link-sandbox-fake"}
    assert adm.get("/banking/link/token/").status_code == 405
    assert std.post("/banking/link/token/").status_code == 403


def test_update_mode_token_uses_the_stored_access_token(adm, plaid_on, fake, field_key):
    item = PlaidItem(institution_name="First Platypus Bank", status=ItemStatus.NEEDS_LOGIN)
    item.set_access_token(TOKEN)
    item.save()
    page = text(adm.get(f"/banking/link/{item.pk}/update/"))
    assert "First Platypus Bank" in page
    r = adm.post(f"/banking/link/{item.pk}/update/token/")
    assert r.json() == {"link_token": "link-sandbox-fake"}
    assert fake.link_updates == [TOKEN]
    assert TOKEN not in page and TOKEN not in r.content.decode()


def test_exchange_stores_an_encrypted_token_and_points_to_the_map(adm, plaid_on, fake, field_key):
    r = adm.post("/banking/link/exchange/", json.dumps({"public_token": "public-x", "institution_name": "First Platypus"}),
                 content_type="application/json")
    item = PlaidItem.objects.get()
    assert r.status_code == 200 and r.json() == {"redirect": f"/banking/link/{item.pk}/map/"}
    assert item.institution_name == "First Platypus" and item.plaid_item_id == "item-fake-1"
    assert item.access_token != TOKEN and TOKEN not in item.access_token
    assert item.get_access_token() == TOKEN
    assert TOKEN not in r.content.decode()


def test_exchange_without_a_field_key_shows_the_message(adm, plaid_on, fake, settings):
    settings.DNB_FIELD_KEY = ""
    r = adm.post("/banking/link/exchange/", json.dumps({"public_token": "public-x"}), content_type="application/json")
    assert r.status_code == 400
    assert r.json()["error"] == "Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts."
    assert PlaidItem.objects.count() == 0


def test_exchange_requires_csrf_and_admin(admin_user, standard_user, plaid_on, fake, field_key):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(admin_user)
    assert strict.post("/banking/link/exchange/", "{}", content_type="application/json").status_code == 403
    std = Client()
    std.force_login(standard_user)
    assert std.post("/banking/link/exchange/", "{}", content_type="application/json").status_code == 403
    assert PlaidItem.objects.count() == 0


@pytest.fixture
def item(db, field_key):
    it = PlaidItem(institution_name="First Platypus Bank")
    it.set_access_token(TOKEN)
    it.save()
    return it


def test_map_page_lists_plaid_accounts_with_ledger_and_kind_selects(adm, plaid_on, fake, item, bank, card):
    page = text(adm.get(f"/banking/link/{item.pk}/map/"))
    assert "Plaid Checking" in page and "···· 0000" in page and "Plaid Credit Card" in page
    assert page.count("<select") >= 4 and "1000 Cash" in page and "2100 Company Visa" in page


def test_map_post_creates_bank_accounts_linked_to_the_item(adm, plaid_on, fake, item, bank, card):
    r = adm.post(f"/banking/link/{item.pk}/map/", {
        "ledger-acct-chk": bank.pk, "kind-acct-chk": "CHECKING", "ledger-acct-card": card.pk, "kind-acct-card": "CARD"})
    assert r.status_code == 302
    chk = BankAccount.objects.get(ledger_account=bank)
    assert (chk.plaid_item, chk.plaid_account_id, chk.mask, chk.kind) == (item, "acct-chk", "0000", "CHECKING")
    assert BankAccount.objects.get(ledger_account=card).plaid_account_id == "acct-card"


def test_map_post_with_a_mismatched_kind_creates_nothing(adm, plaid_on, fake, item, bank, card):
    r = adm.post(f"/banking/link/{item.pk}/map/", {
        "ledger-acct-chk": bank.pk, "kind-acct-chk": "CARD", "ledger-acct-card": card.pk, "kind-acct-card": "CARD"})
    assert r.status_code == 200 and "doesn't fit the ledger account" in text(r)
    assert BankAccount.objects.count() == 0


def test_map_updates_an_existing_bank_account_instead_of_duplicating(adm, plaid_on, fake, item, checking, bank):
    adm.post(f"/banking/link/{item.pk}/map/", {"ledger-acct-chk": bank.pk, "kind-acct-chk": "CHECKING"})
    assert BankAccount.objects.count() == 1
    checking.refresh_from_db()
    assert checking.plaid_item == item and checking.plaid_account_id == "acct-chk"


def test_relinked_clears_the_status_and_syncs(adm, plaid_on, fake, item, checking):
    item.status, item.last_error = ItemStatus.NEEDS_LOGIN, "login changed"
    item.save()
    checking.plaid_item, checking.plaid_account_id = item, "acct-chk"
    checking.save()
    r = adm.post(f"/banking/link/{item.pk}/relinked/")
    assert r.status_code == 200 and r.json() == {"redirect": "/banking/"}
    item.refresh_from_db()
    assert item.status == ItemStatus.OK and item.last_error == ""
    assert BankTransaction.objects.filter(bank_account=checking).count() == 3


def test_sync_now_runs_the_sync_and_reports_counts(adm, std, plaid_on, fake, item, checking, card_account):
    checking.plaid_item, checking.plaid_account_id = item, "acct-chk"
    checking.save()
    assert std.post(f"/banking/sync/{item.pk}/").status_code == 403
    r = adm.post(f"/banking/sync/{item.pk}/", follow=True)
    assert "First Platypus Bank: 3 new transactions" in text(r)
    assert BankTransaction.objects.filter(bank_account=checking).count() == 3


def test_sync_now_shows_the_plaid_problem(adm, plaid_on, monkeypatch, item):
    from banking.plaid_client import PlaidError

    monkeypatch.setattr(plaid_client, "get_client", lambda: FakePlaidClient(error=PlaidError("X", "Plaid is down.")))
    r = adm.post(f"/banking/sync/{item.pk}/", follow=True)
    assert "Plaid is down." in text(r)


# ---- import ----

def test_import_page_has_the_upload_form(adm, checking):
    page = text(adm.get(f"/banking/{checking.pk}/import/"))
    assert "OFX, QFX, or CSV" in page and 'type="file"' in page and "5 MB" in page


def test_ofx_import_then_the_same_file_again(adm, checking):
    r = adm.post(f"/banking/{checking.pk}/import/", upload("checking.ofx", (FIX / "checking.ofx").read_bytes()),
                 follow=True)
    page = text(r)
    assert "3 added" in page and "0 skipped" in page and "0 possible duplicates" in page
    assert BankTransaction.objects.filter(bank_account=checking).count() == 3
    again = text(adm.post(f"/banking/{checking.pk}/import/",
                          upload("checking.ofx", (FIX / "checking.ofx").read_bytes()), follow=True))
    assert "0 added" in again and "3 skipped" in again


def test_qfx_import_for_the_card(adm, card_account):
    page = text(adm.post(f"/banking/{card_account.pk}/import/",
                         upload("card.qfx", (FIX / "card.qfx").read_bytes()), follow=True))
    assert "3 added" in page
    assert BankTransaction.objects.get(external_id="F001").amount == D("-86.40")


def test_unreadable_ofx_shows_the_message(adm, checking):
    page = text(adm.post(f"/banking/{checking.pk}/import/", upload("bad.ofx", b"nope")))
    assert "That file isn't a readable OFX or QFX statement." in page


def test_wrong_file_type(adm, checking):
    page = text(adm.post(f"/banking/{checking.pk}/import/", upload("notes.pdf", b"%PDF")))
    assert "Upload an OFX, QFX, or CSV file." in page


def test_file_too_large(adm, checking):
    big = upload("big.csv", b"a" * (5 * 1024 * 1024 + 1))
    page = text(adm.post(f"/banking/{checking.pk}/import/", big))
    assert "The file is too large (5 MB limit)." in page
    assert BankTransaction.objects.count() == 0


def test_csv_first_upload_asks_for_the_mapping_with_a_five_row_preview(adm, checking):
    csv_bytes = (FIX / "messy.csv").read_bytes()
    r = adm.post(f"/banking/{checking.pk}/import/", upload("messy.csv", csv_bytes))
    assert r.status_code == 302 and r["Location"] == f"/banking/{checking.pk}/import/map/"
    page = text(adm.get(r["Location"]))
    assert "Date" in page and "Description" in page and "Amount" in page
    assert "ACH DEPOSIT ACME BUILDERS" in page and "1,500.00" in page and "-420.00" in page and "-4.50" in page
    assert page.count('class="preview-row"') == 5
    assert BankTransaction.objects.count() == 0


def test_csv_mapping_is_saved_and_reused(adm, checking):
    csv_bytes = (FIX / "messy.csv").read_bytes()
    adm.post(f"/banking/{checking.pk}/import/", upload("messy.csv", csv_bytes))
    r = adm.post(f"/banking/{checking.pk}/import/map/", {
        "date": "Date", "description": "Description", "amount": "Amount", "debit": "", "credit": "",
        "check_number": "", "date_format": "%m/%d/%Y", "action": "import"}, follow=True)
    assert "5 added" in text(r)
    checking.refresh_from_db()
    assert checking.csv_mapping["date"] == "Date" and checking.csv_mapping["amount"] == "Amount"
    # next month's file: no mapping step
    r = adm.post(f"/banking/{checking.pk}/import/", upload("next.csv", NEXT_MONTH), follow=True)
    assert "3 added" in text(r) and r.redirect_chain[-1][0].startswith(f"/banking/{checking.pk}/import/result/")


def test_csv_with_debit_and_credit_columns(adm, checking):
    adm.post(f"/banking/{checking.pk}/import/", upload("dc.csv", (FIX / "debit_credit.csv").read_bytes()))
    r = adm.post(f"/banking/{checking.pk}/import/map/", {
        "date": "Posting Date", "description": "Payee", "amount": "", "debit": "Debit", "credit": "Credit",
        "check_number": "Check #", "date_format": "%m/%d/%Y", "action": "import"}, follow=True)
    assert "3 added" in text(r)
    assert BankTransaction.objects.get(check_number="2050").amount == D("-420.00")


def test_csv_mapping_needs_an_amount_or_debit_credit(adm, checking):
    adm.post(f"/banking/{checking.pk}/import/", upload("c.csv", (FIX / "checking.csv").read_bytes()))
    page = text(adm.post(f"/banking/{checking.pk}/import/map/", {
        "date": "Date", "description": "Description", "amount": "", "debit": "", "credit": "",
        "date_format": "%m/%d/%Y", "action": "import"}))
    assert "Choose the amount column, or the debit and credit columns." in page
    assert BankTransaction.objects.count() == 0


def test_csv_with_a_bad_row_saves_nothing_and_names_the_row(adm, checking):
    bad = b"Date,Description,Amount\n01/03/2026,ok,1.00\n01/04/2026,oops,twelve\n"
    adm.post(f"/banking/{checking.pk}/import/", upload("bad.csv", bad))
    page = text(adm.post(f"/banking/{checking.pk}/import/map/", {
        "date": "Date", "description": "Description", "amount": "Amount", "date_format": "%m/%d/%Y",
        "action": "import"}))
    assert "Row 3: amount must be a number." in page
    assert BankTransaction.objects.count() == 0
    checking.refresh_from_db()
    assert checking.csv_mapping == {}


def test_preview_action_does_not_import(adm, checking):
    adm.post(f"/banking/{checking.pk}/import/", upload("c.csv", (FIX / "checking.csv").read_bytes()))
    page = text(adm.post(f"/banking/{checking.pk}/import/map/", {
        "date": "Date", "description": "Description", "amount": "Amount", "date_format": "%m/%d/%Y",
        "action": "preview"}))
    assert "CHECK 2050" in page and BankTransaction.objects.count() == 0


def test_mapping_page_without_an_upload_goes_back_to_the_upload(adm, checking):
    r = adm.get(f"/banking/{checking.pk}/import/map/")
    assert r.status_code == 302 and r["Location"] == f"/banking/{checking.pk}/import/"


def test_import_result_page_shows_counts(adm, checking):
    page = text(adm.get(f"/banking/{checking.pk}/import/result/?added=4&skipped=2&dups=1"))
    assert "4 added" in page and "2 skipped" in page and "1 possible duplicate" in page
    assert f"/banking/inbox/?account={checking.pk}" in page

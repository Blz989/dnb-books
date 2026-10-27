import html
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking.models import (
    AccountKind, BankAccount, BankTransaction, Match, PatternType, Rule, Transfer, TransferKind, TransferStatus,
    TxnSource, TxnStatus,
)
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus, JournalEntry
from sales.models import PaymentMethod
from sales.posting.receipts import receive_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


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
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def fees(db):
    return Account.objects.create(number="6900", name="Bank fees", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)


@pytest.fixture
def make_txn(checking):
    counter = iter(range(1, 1000))

    def factory(amount, on=d(1, 10), description="Some bank text", account=None, status=TxnStatus.NEW):
        return BankTransaction.objects.create(
            bank_account=account or checking, external_id=f"x{next(counter)}", date=on, description=description,
            amount=D(amount), source=TxnSource.FILE, status=status)

    return factory


@pytest.fixture
def ach(setup, admin_user, bank, customer):
    return receive_payment(customer=customer, date=d(1, 8), method=PaymentMethod.ACH, amount=D("75.00"),
                           user=admin_user, bank_account=bank)


def token(page, name="token"):
    import re
    return re.search(rf'name="{name}" value="([^"]+)"', page).group(1)


# ---- inbox list ----

def test_pages_need_login(client):
    for path in ("/banking/inbox/", "/banking/rules/", "/banking/transfers/", "/banking/transfers/new/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


def test_inbox_shows_new_rows_with_suggestion_and_score(adm, ach, make_txn, fees):
    Rule.objects.create(pattern="service fee", account=fees)
    make_txn("75.00", on=d(1, 9), description="ACH CREDIT ACME")
    make_txn("-15.00", description="MONTHLY SERVICE FEE")
    make_txn("-3.33", description="MYSTERY")
    make_txn("-1.00", description="ARCHIVED ROW", status=TxnStatus.EXCLUDED)
    page = text(adm.get("/banking/inbox/"))
    assert "ACH CREDIT ACME" in page and "Payment PMT-00001 from Acme Builders" in page and "79" in page
    assert "Book to 6900 Bank fees" in page and "50" in page
    assert "MYSTERY" in page and "No suggestion" in page
    assert "ARCHIVED ROW" not in page  # default filter is New
    assert "Confirm" in page and "Change" in page and "Exclude" in page and "Transfer" in page


def test_filters_by_account_and_status(adm, setup, make_txn, card_account):
    make_txn("-1.00", description="on checking")
    make_txn("-2.00", description="on the card", account=card_account)
    make_txn("-3.00", description="was excluded", status=TxnStatus.EXCLUDED)
    only_card = text(adm.get(f"/banking/inbox/?account={card_account.pk}"))
    assert "on the card" in only_card and "on checking" not in only_card
    excluded = text(adm.get("/banking/inbox/?status=EXCLUDED"))
    assert "was excluded" in excluded and "on checking" not in excluded and "Restore" in excluded
    everything = text(adm.get("/banking/inbox/?status=ALL"))
    assert all(s in everything for s in ("on checking", "on the card", "was excluded"))


def test_matched_rows_show_what_they_matched(adm, ach, make_txn, admin_user):
    from banking import inbox

    txn = make_txn("75.00", on=d(1, 9), description="ACH CREDIT")
    inbox.confirm(txn, inbox.suggest(txn), user=admin_user)
    page = text(adm.get("/banking/inbox/?status=MATCHED"))
    assert "ACH CREDIT" in page and "PMT-00001" in page


def test_empty_inbox(adm, setup):
    assert "Nothing waiting" in text(adm.get("/banking/inbox/"))


# ---- actions ----

def test_confirm_links_a_document(adm, ach, make_txn):
    txn = make_txn("75.00", on=d(1, 9))
    page = text(adm.get("/banking/inbox/"))
    r = adm.post(f"/banking/inbox/{txn.pk}/confirm/", {"token": token(page)}, follow=True)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED and txn.match.sales_payment == ach
    assert "Matched to Payment PMT-00001" in text(r)


def test_double_confirm_posts_once(adm, setup, make_txn, fees):
    Rule.objects.create(pattern="fee", account=fees)
    txn = make_txn("-15.00", description="service fee")
    tok = token(text(adm.get("/banking/inbox/")))
    adm.post(f"/banking/inbox/{txn.pk}/confirm/", {"token": tok})
    before = JournalEntry.objects.count()
    r = adm.post(f"/banking/inbox/{txn.pk}/confirm/", {"token": tok}, follow=True)
    assert JournalEntry.objects.count() == before
    assert "already" in text(r).lower()
    assert Match.objects.count() == 1


def test_change_to_another_candidate(adm, ach, make_txn, fees):
    Rule.objects.create(pattern="acme", account=fees)
    txn = make_txn("75.00", on=d(1, 9), description="ACME")
    page = text(adm.get(f"/banking/inbox/{txn.pk}/change/"))
    assert "PMT-00001" in page and "Book to 6900 Bank fees" in page
    key = f"rule:banking.rule:{Rule.objects.get().pk}"
    r = adm.post(f"/banking/inbox/{txn.pk}/change/", {"token": token(page), "action": "candidate", "candidate": key},
                 follow=True)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.BOOKED and txn.match.journal_entry.lines.filter(account=fees).exists()
    assert r.status_code == 200


def test_change_choose_the_document_instead_of_the_first(adm, ach, make_txn, fees):
    Rule.objects.create(pattern="acme", account=fees)
    txn = make_txn("75.00", on=d(1, 9), description="ACME")
    page = text(adm.get(f"/banking/inbox/{txn.pk}/change/"))
    adm.post(f"/banking/inbox/{txn.pk}/change/", {"token": token(page), "action": "candidate",
                                                   "candidate": f"document:sales.payment:{ach.pk}"})
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED


def test_stale_candidate_is_refused(adm, ach, make_txn):
    txn = make_txn("75.00", on=d(1, 9))
    page = text(adm.get(f"/banking/inbox/{txn.pk}/change/"))
    r = adm.post(f"/banking/inbox/{txn.pk}/change/", {"token": token(page), "action": "candidate",
                                                      "candidate": "document:sales.payment:99999"})
    assert "That suggestion is no longer available." in text(r)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW


def test_new_entry_with_an_account(adm, setup, make_txn, fees, bank):
    txn = make_txn("-22.50", description="WIRE FEE")
    page = text(adm.get(f"/banking/inbox/{txn.pk}/change/"))
    adm.post(f"/banking/inbox/{txn.pk}/change/", {
        "token": token(page), "action": "entry", "account": fees.pk, "memo": "Wire fee"}, follow=True)
    txn.refresh_from_db()
    entry = txn.match.journal_entry
    assert txn.status == TxnStatus.BOOKED and entry.memo == "Wire fee" and entry.status == EntryStatus.POSTED
    assert sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account")) == [
        ("1000", D("0.00"), D("22.50")), ("6900", D("22.50"), D("0.00"))]


def test_new_entry_without_an_account_shows_a_message(adm, setup, make_txn):
    txn = make_txn("-22.50")
    page = text(adm.get(f"/banking/inbox/{txn.pk}/change/"))
    r = adm.post(f"/banking/inbox/{txn.pk}/change/", {"token": token(page), "action": "entry", "account": ""})
    assert "Choose the account to book it to." in text(r)
    assert Match.objects.count() == 0


def test_exclude_needs_a_reason_then_restore(adm, setup, make_txn):
    txn = make_txn("-9.00")
    page = text(adm.get("/banking/inbox/"))
    r = adm.post(f"/banking/inbox/{txn.pk}/exclude/", {"token": token(page), "reason": " "}, follow=True)
    assert "Enter a reason." in text(r)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW
    adm.post(f"/banking/inbox/{txn.pk}/exclude/", {"token": token(page), "reason": "Personal"})
    txn.refresh_from_db()
    assert (txn.status, txn.exclude_reason) == (TxnStatus.EXCLUDED, "Personal")
    adm.post(f"/banking/inbox/{txn.pk}/restore/")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW


def test_bulk_confirm_reports_the_count(adm, ach, make_txn, fees):
    Rule.objects.create(pattern="fee", account=fees)
    a = make_txn("75.00", on=d(1, 9))
    b = make_txn("-15.00", description="fee")
    c = make_txn("-3.33", description="???")
    page = text(adm.get("/banking/inbox/"))
    r = adm.post("/banking/inbox/bulk/", {"token": token(page), "rows": [a.pk, b.pk, c.pk]}, follow=True)
    assert "Confirmed 2 of 3 selected." in text(r)
    c.refresh_from_db()
    assert c.status == TxnStatus.NEW


def test_bulk_confirm_with_nothing_ticked(adm, setup):
    r = adm.post("/banking/inbox/bulk/", {"token": "t1"}, follow=True)
    assert "Tick at least one row." in text(r)


def test_standard_user_can_work_the_inbox(std, ach, make_txn):
    txn = make_txn("75.00", on=d(1, 9))
    page = text(std.get("/banking/inbox/"))
    std.post(f"/banking/inbox/{txn.pk}/confirm/", {"token": token(page)})
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED


# ---- rules ----

def test_rules_list_add_edit_toggle_and_order(adm, std, fees, expense, make_txn, setup):
    assert std.get("/banking/rules/").status_code == 200
    r = adm.post("/banking/rules/new/", {"pattern": "FEE", "pattern_type": "CONTAINS", "account": fees.pk,
                                          "memo": "Bank fee", "bank_account": "", "is_active": "on"})
    assert r.status_code == 302
    adm.post("/banking/rules/new/", {"pattern": "FEE", "pattern_type": "CONTAINS", "account": expense.pk,
                                      "memo": "Other", "bank_account": "", "is_active": "on"})
    first, second = Rule.objects.order_by("position", "id")
    assert first.position < second.position
    txn = make_txn("-5.00", description="wire fee")
    from banking import inbox

    assert inbox.suggest(txn).target == first
    adm.post(f"/banking/rules/{second.pk}/move/", {"direction": "up"})
    assert inbox.suggest(txn).target == second
    adm.post(f"/banking/rules/{second.pk}/toggle/")
    second.refresh_from_db()
    assert not second.is_active and inbox.suggest(txn).target == first
    adm.post(f"/banking/rules/{first.pk}/edit/", {"pattern": "WIRE FEE", "pattern_type": "STARTS_WITH",
                                                   "account": fees.pk, "memo": "x", "bank_account": ""})
    first.refresh_from_db()
    assert (first.pattern, first.pattern_type, first.is_active) == ("WIRE FEE", "STARTS_WITH", False)
    page = text(adm.get("/banking/rules/"))
    assert "WIRE FEE" in page and "Starts with" in page


def test_rule_needs_a_pattern(adm, fees):
    r = adm.post("/banking/rules/new/", {"pattern": " ", "pattern_type": "CONTAINS", "account": fees.pk})
    assert r.status_code == 200 and Rule.objects.count() == 0


# ---- transfers ----

@pytest.fixture
def savings(db):
    acct = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    return BankAccount.objects.create(ledger_account=acct, kind=AccountKind.SAVINGS)


def test_transfer_form_prefills_from_an_inbox_row(adm, setup, checking, card_account, make_txn):
    txn = make_txn("-500.00", on=d(1, 20), description="CARD PAYMENT")
    page = text(adm.get(f"/banking/transfers/new/?from_txn={txn.pk}"))
    assert 'value="500.00"' in page and 'value="2026-01-20"' in page
    assert f'name="from_txn" value="{txn.pk}"' in page
    assert f'<option value="{checking.pk}" selected' in page


def test_money_in_row_prefills_the_destination(adm, setup, checking, savings, make_txn):
    txn = make_txn("2000.00", on=d(1, 9), account=savings)
    page = text(adm.get(f"/banking/transfers/new/?from_txn={txn.pk}"))
    assert f'<option value="{savings.pk}" selected' in page


def test_creating_a_transfer_from_an_inbox_row_matches_it(adm, setup, checking, savings, make_txn, bank):
    txn = make_txn("-2000.00", on=d(1, 9))
    page = text(adm.get(f"/banking/transfers/new/?from_txn={txn.pk}"))
    r = adm.post("/banking/transfers/new/", {
        "token": token(page), "from_txn": txn.pk, "date": "2026-01-09", "kind": "TRANSFER",
        "from_account": checking.pk, "to_account": savings.pk, "amount": "2000.00"}, follow=True)
    assert r.status_code == 200
    t = Transfer.objects.get()
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED and txn.match.journal_entry == t.journal_entry
    assert "TR-%d" % t.pk in text(r)


def test_double_submit_creates_one_transfer(adm, setup, checking, savings):
    page = text(adm.get("/banking/transfers/new/"))
    data = {"token": token(page), "date": "2026-01-09", "kind": "TRANSFER", "from_account": checking.pk,
            "to_account": savings.pk, "amount": "100.00"}
    adm.post("/banking/transfers/new/", data)
    adm.post("/banking/transfers/new/", data)
    assert Transfer.objects.count() == 1


def test_loan_payment_form_posts_principal_and_interest(adm, setup, checking, loan, interest_expense):
    page = text(adm.get("/banking/transfers/new/"))
    assert "Principal" in page and "Interest account" in page
    r = adm.post("/banking/transfers/new/", {
        "token": token(page), "date": "2026-01-15", "kind": "LOAN_PAYMENT", "from_account": checking.pk,
        "to_account": loan.pk, "amount": "400.00", "principal": "340.00", "interest": "60.00",
        "interest_account": interest_expense.pk})
    assert r.status_code == 302
    t = Transfer.objects.get()
    assert (t.principal, t.interest, t.interest_account) == (D("340.00"), D("60.00"), interest_expense)


def test_transfer_errors_show_exact_messages(adm, setup, checking, loan, interest_expense, savings):
    page = text(adm.get("/banking/transfers/new/"))
    bad = adm.post("/banking/transfers/new/", {
        "token": token(page), "date": "2026-01-15", "kind": "LOAN_PAYMENT", "from_account": checking.pk,
        "to_account": loan.pk, "amount": "400.00", "principal": "340.00", "interest": "59.00",
        "interest_account": interest_expense.pk})
    assert "Principal and interest must add up to 400.00." in text(bad)
    same = adm.post("/banking/transfers/new/", {
        "token": "t2", "date": "2026-01-15", "kind": "TRANSFER", "from_account": checking.pk,
        "to_account": checking.pk, "amount": "5.00"})
    assert "Choose two different accounts." in text(same)
    payoff = adm.post("/banking/transfers/new/", {
        "token": "t3", "date": "2026-01-15", "kind": "CARD_PAYOFF", "from_account": checking.pk,
        "to_account": savings.pk, "amount": "5.00"})
    assert "A card payoff goes to a credit card account." in text(payoff)
    assert Transfer.objects.count() == 0


def test_transfer_list_and_admin_only_void(adm, std, setup, checking, savings, admin_user):
    from banking.transfers import create_transfer

    t = create_transfer(date=d(1, 9), from_account=checking, to_account=savings, amount=D("100.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)
    page = text(adm.get("/banking/transfers/"))
    assert f"TR-{t.pk}" in page and "100.00" in page and "Void" in page
    assert "Void" not in text(std.get("/banking/transfers/")).replace("Voided", "")
    assert std.post(f"/banking/transfers/{t.pk}/void/", {"reason": "x"}).status_code == 403
    assert "Enter a reason." in text(adm.post(f"/banking/transfers/{t.pk}/void/", {"reason": ""}))
    r = adm.post(f"/banking/transfers/{t.pk}/void/", {"reason": "Wrong account"}, follow=True)
    t.refresh_from_db()
    assert t.status == TransferStatus.VOID and "Void" in text(r)

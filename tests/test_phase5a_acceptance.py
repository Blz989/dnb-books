"""Spec section 1: a scripted month through the screens, with every ledger check at zero after each step, plus the
home screen cards."""
import html
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from banking import reconcile
from banking.models import (
    BankAccount, BankTransaction, CheckNumber, CheckStatus, ItemStatus, Match, PlaidItem, ReconStatus, Reconciliation,
    TxnStatus,
)
from contacts.models import Vendor
from items.models import ItemType
from items.stock import check_invariants
from ledger.balances import account_totals
from ledger.errors import EntryStateError
from ledger.models import Account, AccountSubtype, AccountType
from ledger.posting import LineSpec, post_entry, void_entry
from purchasing.checks import ap_check
from purchasing.models import VendorPayment, VendorPaymentMethod as VM
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import PayLine, pay_bills, pay_vendor
from sales.checks import ar_check, undeposited_check
from sales.models import PaymentMethod
from sales.posting.deposits import make_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "bank"


def d(month, day):
    return date(2026, month, day)


def text(response):
    return html.unescape(response.content.decode())


def checks():
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []
    for day in range(1, 32):
        as_of = date(2026, 1, day)
        assert ap_check(as_of) == [] and ar_check(as_of) == [] and undeposited_check(as_of) == [], as_of
    totals = account_totals(start=None, end=date.max)
    assert sum(v[0] for v in totals.values()) == sum(v[1] for v in totals.values())


def token(page, nth=0):
    return re.findall(r'name="token" value="([^"]+)"', page)[nth]


def balance(account):
    debit, credit = account_totals(start=None, end=date.max).get(account.pk, (D("0.00"), D("0.00")))
    return debit - credit


EXTRA_OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>20260131120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1002<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>121000248<ACCTID>000001234<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20260131
<STMTTRN><TRNTYPE>DEP<DTPOSTED>20260113120000<TRNAMT>450.00<FITID>C004<NAME>DEPOSIT</STMTTRN>
<STMTTRN><TRNTYPE>PAYMENT<DTPOSTED>20260115120000<TRNAMT>-400.00<FITID>C005<NAME>LOAN PAYMENT 5502</STMTTRN>
<STMTTRN><TRNTYPE>PAYMENT<DTPOSTED>20260120120000<TRNAMT>-500.00<FITID>C006<NAME>CARD PAYMENT</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>0.00<DTASOF>20260131120000</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.fixture
def world(sales_settings, purchasing_settings, inv_settings, admin_user, make_item, bank, checking, card, card_account,
          loan, interest_expense, customer, vendor, expense, equity):
    service = make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None,
                        inventory_account=None)
    fees = Account.objects.create(number="6900", name="Bank fees", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)
    checking.next_check_number = 2050
    checking.save()
    client = Client()
    client.force_login(admin_user)
    return client, service, fees


def test_phase5a_scripted_month(world, admin_user, bank, checking, card, card_account, loan, interest_expense,
                                customer, vendor, expense, equity, ap):
    client, service, fees = world
    checks()

    # The books before the bank files arrive.
    post_entry(date=d(1, 1), user=admin_user, memo="Owner investment", lines=[
        LineSpec(bank, debit=D("5000.00")), LineSpec(equity, credit=D("5000.00"))])
    inv1 = create_invoice(customer=customer, date=d(1, 2), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("1500.00"))])
    inv2 = create_invoice(customer=customer, date=d(1, 2), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("450.00"))])
    ach = receive_payment(customer=customer, date=d(1, 3), method=PaymentMethod.ACH, amount=D("1500.00"),
                          user=admin_user, bank_account=bank, applications=[(inv1, D("1500.00"))])
    cheque = receive_payment(customer=customer, date=d(1, 12), method=PaymentMethod.CHECK, amount=D("300.00"),
                             user=admin_user, applications=[(inv2, D("300.00"))])
    cash = receive_payment(customer=customer, date=d(1, 12), method=PaymentMethod.CASH, amount=D("150.00"),
                           user=admin_user, applications=[(inv2, D("150.00"))])
    deposit = make_deposit(payments=[cheque, cash], bank_account=bank, date=d(1, 13), user=admin_user)
    supplier_bill = create_bill(vendor=vendor, date=d(1, 2), vendor_invoice_number="AS-100", user=admin_user,
                                expense_lines=[BillExpenseSpec(expense, D("420.00"))])
    [vendor_check] = pay_bills(date=d(1, 5), method=VM.CHECK, paid_from=bank, user=admin_user,
                               lines=[PayLine(supplier_bill, D("420.00"))])
    pay_vendor(vendor=vendor, date=d(1, 2), method=VM.CARD, amount=D("1000.00"), paid_from=card, user=admin_user)
    checks()
    assert vendor_check.check_number == ""

    # 1. The vendor check is printed on the stock: check 2050.
    page = text(client.get(f"/banking/checks/{vendor_check.number}/print/"))
    assert 'value="2050"' in page
    r = client.post(f"/banking/checks/{vendor_check.number}/confirm/", {
        "number": "2050", "result": "yes", "token": token(page)})
    assert r.status_code == 302
    vendor_check.refresh_from_db()
    assert vendor_check.check_number == "2050"
    assert CheckNumber.objects.get().status == CheckStatus.PRINTED
    checks()

    # 2. Import a statement file for checking, one for the card, and the rest of checking's month.
    for account, name, data in (
        (checking, "checking.ofx", (FIX / "checking.ofx").read_bytes()),
        (checking, "more.ofx", EXTRA_OFX.encode()),
        (card_account, "card.qfx", (FIX / "card.qfx").read_bytes()),
    ):
        r = client.post(f"/banking/{account.pk}/import/", {"file": SimpleUploadedFile(name, data)}, follow=True)
        assert r.status_code == 200 and "added" in text(r)
    assert BankTransaction.objects.filter(bank_account=checking).count() == 6
    assert BankTransaction.objects.filter(bank_account=card_account).count() == 3
    checks()

    # 3. Match the customer ACH payment, the deposit, and the printed vendor check.
    row = {t.external_id: t for t in BankTransaction.objects.all()}
    for key, expect in (("C001", ach), ("C004", deposit), ("C002", vendor_check)):
        txn = row[key]
        inbox_page = text(client.get(f"/banking/inbox/?account={checking.pk}"))
        tok = re.search(rf'/banking/inbox/{txn.pk}/confirm/" style="display:inline">.*?name="token" value="([^"]+)"',
                        inbox_page, re.S).group(1)
        client.post(f"/banking/inbox/{txn.pk}/confirm/", {"token": tok})
        txn.refresh_from_db()
        assert txn.status == TxnStatus.MATCHED, key
        assert expect in (txn.match.sales_payment, txn.match.deposit, txn.match.vendor_payment)
    assert row["C002"].match.vendor_payment == vendor_check  # by check number, not just amount
    checks()

    # 4. Book the bank fee by rule.
    before = balance(bank)
    client.post("/banking/rules/new/", {"pattern": "SERVICE FEE", "pattern_type": "CONTAINS", "account": fees.pk,
                                         "memo": "Monthly bank fee", "bank_account": "", "is_active": "on"})
    fee = row["C003"]
    inbox_page = text(client.get(f"/banking/inbox/?account={checking.pk}"))
    assert "Book to 6900 Bank fees" in inbox_page
    tok = re.search(rf'/banking/inbox/{fee.pk}/confirm/" style="display:inline">.*?name="token" value="([^"]+)"',
                    inbox_page, re.S).group(1)
    client.post(f"/banking/inbox/{fee.pk}/confirm/", {"token": tok})
    fee.refresh_from_db()
    assert fee.status == TxnStatus.BOOKED and balance(bank) == before - D("15.00") and balance(fees) == D("15.00")
    checks()

    # 5. Card payoff: created from the checking row, then the card's row matches the same transfer. Booked once.
    payoff_out, payoff_in = row["C006"], row["F003"]
    page = text(client.get(f"/banking/transfers/new/?from_txn={payoff_out.pk}"))
    r = client.post("/banking/transfers/new/", {
        "token": token(page), "from_txn": payoff_out.pk, "date": "2026-01-20", "kind": "CARD_PAYOFF",
        "from_account": checking.pk, "to_account": card_account.pk, "amount": "500.00"})
    assert r.status_code == 302
    payoff_out.refresh_from_db()
    assert payoff_out.status == TxnStatus.MATCHED
    after_transfer = (balance(bank), balance(card))
    inbox_page = text(client.get(f"/banking/inbox/?account={card_account.pk}"))
    assert "Transfer Cash to Company Visa" in inbox_page and ">75<" in inbox_page
    tok = re.search(rf'/banking/inbox/{payoff_in.pk}/confirm/" style="display:inline">.*?name="token" value="([^"]+)"',
                    inbox_page, re.S).group(1)
    client.post(f"/banking/inbox/{payoff_in.pk}/confirm/", {"token": tok})
    payoff_in.refresh_from_db()
    assert payoff_in.status == TxnStatus.MATCHED and (balance(bank), balance(card)) == after_transfer
    assert payoff_in.match.journal_entry == payoff_out.match.journal_entry
    checks()

    # The card's two purchases are booked to an expense account from the Change page.
    for key in ("F001", "F002"):
        txn = row[key]
        page = text(client.get(f"/banking/inbox/{txn.pk}/change/"))
        client.post(f"/banking/inbox/{txn.pk}/change/", {
            "token": token(page), "action": "entry", "account": expense.pk, "memo": txn.description})
        txn.refresh_from_db()
        assert txn.status == TxnStatus.BOOKED
    checks()

    # 6. A loan payment with principal and interest, created from the checking row.
    loan_row = row["C005"]
    page = text(client.get(f"/banking/transfers/new/?from_txn={loan_row.pk}"))
    r = client.post("/banking/transfers/new/", {
        "token": token(page), "from_txn": loan_row.pk, "date": "2026-01-15", "kind": "LOAN_PAYMENT",
        "from_account": checking.pk, "to_account": loan.pk, "amount": "400.00", "principal": "340.00",
        "interest": "60.00", "interest_account": interest_expense.pk})
    assert r.status_code == 302
    assert balance(loan.ledger_account) == D("340.00") and balance(interest_expense) == D("60.00")
    checks()
    assert not BankTransaction.objects.filter(status=TxnStatus.NEW).exists()

    # 7. Reconcile checking and the card to their statements at 0.00 difference.
    # Checking: 5000 + 1500 - 420 - 15 + 450 - 400 - 500
    r = client.post(f"/banking/reconcile/{checking.pk}/", {"statement_date": "2026-01-31", "statement_balance": "5615.00"})
    rec = Reconciliation.objects.get(bank_account=checking)
    assert r.status_code == 302
    page = text(client.get(r["Location"]))
    assert 'class="difference warn"' in page  # the opening investment is not matched to a bank row
    client.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/ticks/", {"cleared": [i.pk for i in rec.items.all()]})
    assert reconcile.difference(rec) == D("0.00")
    client.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/")
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED and rec.report_pdf.read(4) == b"%PDF"

    # Card: 1000 + 86.40 + 230.00 - 500.00 owed, typed as a positive amount.
    r = client.post(f"/banking/reconcile/{card_account.pk}/", {"statement_date": "2026-01-31", "statement_balance": "816.40"})
    card_rec = Reconciliation.objects.get(bank_account=card_account)
    client.post(f"/banking/reconcile/{card_account.pk}/{card_rec.pk}/ticks/", {"cleared": [i.pk for i in card_rec.items.all()]})
    assert reconcile.difference(card_rec) == D("0.00")
    client.post(f"/banking/reconcile/{card_account.pk}/{card_rec.pk}/finish/")
    card_rec.refresh_from_db()
    assert card_rec.status == ReconStatus.FINISHED
    checks()

    # What was cleared is locked, from every direction.
    from purchasing.posting.payments import void_vendor_payment

    with pytest.raises(EntryStateError, match=f"{vendor_check.number} is part of the January 2026 reconciliation"):
        void_vendor_payment(vendor_check, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match="An admin must undo that reconciliation first."):
        void_entry(fee.match.journal_entry, user=admin_user, reason="x")


# ---- home screen cards ----

@pytest.fixture
def home(admin_user):
    client = Client()
    client.force_login(admin_user)
    return client


def test_home_inbox_card_counts_new_rows_and_links_to_the_inbox(home, checking, card_account):
    for i, status in enumerate((TxnStatus.NEW, TxnStatus.NEW, TxnStatus.EXCLUDED)):
        BankTransaction.objects.create(bank_account=checking, external_id=f"e{i}", date=date(2026, 1, 5),
                                       description="x", amount=D("1.00"), source="FILE", status=status)
    page = text(home.get("/"))
    assert "Inbox items waiting" in page and 'href="/banking/inbox/"' in page
    assert re.search(r"Inbox items waiting</div><div class=\"v\">2<", page)


def test_home_sync_card_without_linked_banks(home, checking):
    page = text(home.get("/"))
    assert "Bank sync" in page and "No banks linked" in page


def test_home_sync_card_shows_last_sync_and_warns_when_stale(home, checking, field_key):
    item = PlaidItem(institution_name="First Platypus Bank", last_sync_at=timezone.now() - timedelta(hours=3))
    item.set_access_token("t")
    item.save()
    fresh = text(home.get("/"))
    assert "Last sync" in fresh and 'class="kpi warn"><div class="l">Bank sync' not in fresh
    item.last_sync_at = timezone.now() - timedelta(hours=30)
    item.save()
    stale = text(home.get("/"))
    assert 'class="kpi warn"><div class="l">Bank sync' in stale and "over 24 hours" in stale


def test_home_sync_card_warns_for_a_bank_that_needs_login_and_offers_relink(home, checking, field_key):
    item = PlaidItem(institution_name="First Platypus Bank", status=ItemStatus.NEEDS_LOGIN,
                     last_sync_at=timezone.now())
    item.set_access_token("t")
    item.save()
    page = text(home.get("/"))
    assert 'class="kpi warn"><div class="l">Bank sync' in page
    assert "First Platypus Bank needs you to sign in again" in page
    assert f'/banking/link/{item.pk}/update/' in page and "Re-link" in page


def test_home_sync_card_warns_for_a_bank_never_synced(home, checking, field_key):
    item = PlaidItem(institution_name="First Platypus Bank")
    item.set_access_token("t")
    item.save()
    assert "Not synced yet" in text(home.get("/"))


def test_home_lists_accounts_not_reconciled_in_40_days(home, checking, card_account, admin_user, bank, equity):
    old = timezone.localdate() - timedelta(days=60)
    post_entry(date=old, user=admin_user, lines=[LineSpec(bank, debit=D("10.00")), LineSpec(equity, credit=D("10.00"))])
    # Checking has activity and was never reconciled. The card has none.
    page = text(home.get("/"))
    assert "Not reconciled in 40 days" in page and "Cash" in page and "Company Visa" not in page
    Reconciliation.objects.create(bank_account=checking, statement_date=timezone.localdate() - timedelta(days=39),
                                  statement_balance=D("10.00"), status=ReconStatus.FINISHED, started_by=admin_user)
    assert "Cash" not in text(home.get("/")).split("Not reconciled in 40 days")[1].split("</div></div>")[0]
    Reconciliation.objects.filter(bank_account=checking).update(
        statement_date=timezone.localdate() - timedelta(days=41))
    assert "Cash" in text(home.get("/")).split("Not reconciled in 40 days")[1].split("</div></div>")[0]


def test_home_shows_no_banking_cards_problem_when_nothing_is_set_up(home, db):
    page = text(home.get("/"))
    assert "Inbox items waiting" in page and "Bank sync" in page

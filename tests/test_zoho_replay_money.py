from datetime import date
from decimal import Decimal

import pytest

from banking.models import BankAccount, CheckNumber, CheckStatus
from items.models import MovementKind, StockMovement
from items.stock import check_invariants
from ledger.models import JournalEntry, JournalLine
from migration.models import ZohoRecord
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from purchasing.balances import bill_balance
from purchasing.checks import ap_check
from purchasing.models import (
    Bill,
    VendorCredit,
    VendorCreditKind,
    VendorPayment,
    VendorPaymentMethod,
)
from sales.balances import invoice_balance, payment_unapplied
from sales.checks import ar_check, undeposited_check
from sales.models import CreditNote, Deposit, Invoice, Payment, PaymentMethod
from sales.posting.deposits import undeposited_payments
from tests.zoho_sample import BANK, CARD, EXPECTED, UNDEPOSITED, build_sample_export


def _replay(admin_user, tmp_path, *, extra_rows=None, pairs=None, after_each=None):
    r = Replay(read_export(build_sample_export(tmp_path, extra_rows=extra_rows)), admin_user)
    r.load_master()
    if pairs is not None:
        r.load_deposit_pairs(pairs)
    for event in r.events():
        r.post(event)
        if after_each is not None:
            after_each(event)
    return r


@pytest.fixture
def replay(db, admin_user, tmp_path):
    return _replay(admin_user, tmp_path)


def _entry(source_type, source_id):
    entry = JournalEntry.objects.get(source_type=source_type, source_id=source_id)
    lines = JournalLine.objects.in_ledger().filter(entry=entry).select_related("account")
    return {l.account.name: l.debit - l.credit for l in lines}


# 20 small cash payments dated after the first transfer: with payment "2" they fill the 20
# candidates of a 2025-06-30 transfer, so payment "4" is out of reach of the search.
SMALL_CASH = [{
    "CustomerPayment ID": f"541000000{n:02d}", "Payment Number": f"CP-20{n:02d}", "Mode": "Cash",
    "Date": f"2025-01-{n:02d}", "Amount": "1.00", "Unused Amount": "1.00", "Bank Charges": "0.00",
    "Reference Number": "", "Customer Name": "Acme Hardware", "Deposit To": UNDEPOSITED,
    "Invoice Number": "", "Amount Applied to Invoice": "0.00", "Invoice Payment Applied Date": "",
} for n in range(1, 21)]
LATE_TRANSFER = {
    "Transaction Date": "2025-06-30", "Transaction Type": "Funds Transfer", "From Account": UNDEPOSITED,
    "To Account": BANK, "Reference": "DEP-2", "Description": "Deposit", "Total": "1,307.50",
}  # = payment "2" (1012.50) + payment "4" (295.00)
UNMATCHED = {"customer_payments": SMALL_CASH, "transfers": [LATE_TRANSFER]}


def test_payments_to_undeposited_and_to_bank(replay):
    check = Payment.objects.get(number="2")
    assert check.method == PaymentMethod.CHECK and check.deposited_to.name == UNDEPOSITED
    assert check.reference == "CHK 4411"
    cash = Payment.objects.get(number="4")
    assert cash.method == PaymentMethod.CASH and cash.deposited_to.name == UNDEPOSITED
    ach = Payment.objects.get(number="1001")
    assert ach.method == PaymentMethod.ACH and ach.deposited_to.name == BANK
    assert ach.reference == "Bank Transfer ACH 9912"  # Zoho's Mode kept in the reference
    assert set(undeposited_payments().values_list("number", flat=True)) == {"2", "4"}
    assert invoice_balance(Invoice.objects.get(number="INV-000003")) == 0
    assert ZohoRecord.objects.get(kind="customer_payment", zoho_id="54000000003").target == ach


def test_mixed_payment_numbers_kept(replay):
    numbers = set(Payment.objects.values_list("number", flat=True))
    assert {"1001", "CP-1002", "57"} <= numbers
    assert numbers == {"1", "2", "1001", "CP-1002", "3", "57", "4", "CP-1003"}
    assert set(VendorPayment.objects.values_list("number", flat=True)) == {"1", "2"}


def test_unused_amount_stays_unapplied(replay):
    payment = Payment.objects.get(number="57")
    assert payment.amount == Decimal("600.00")
    assert payment_unapplied(payment) == Decimal("60.50")
    assert invoice_balance(Invoice.objects.get(number="INV-000006")) == 0


def test_bank_charges_entry(replay):
    assert _entry("migration.expense", "54000000008") == {
        "Bank Fees and Charges": Decimal("3.00"), BANK: Decimal("-3.00")}
    assert Payment.objects.get(number="CP-1003").amount == Decimal("321.00")


def test_vendor_payment_check_number_registered_handwritten(replay):
    check = VendorPayment.objects.get(number="1")
    assert check.method == VendorPaymentMethod.CHECK and check.check_number == "5001"
    assert check.paid_from.name == BANK
    row = CheckNumber.objects.get(bank_account=BankAccount.objects.get(ledger_account=check.paid_from))
    assert (row.number, row.status, row.vendor_payment) == ("5001", CheckStatus.HANDWRITTEN, check)
    ach = VendorPayment.objects.get(number="2")
    assert ach.method == VendorPaymentMethod.ACH and ach.check_number == ""
    for number in ("BILL-001", "BILL-002"):
        assert bill_balance(Bill.objects.get(vendor_invoice_number=number)) == 0


def test_credit_note_and_vendor_credit_applied(replay):
    note = CreditNote.objects.get(number="CN-000001")
    line = note.lines.get()
    assert (line.description, line.amount, line.account.name) == ("Red Widget", Decimal("60.00"), "Sales")
    app = note.applications.get()
    assert (app.invoice.number, app.amount, app.applied_on) == ("INV-000006", Decimal("60.00"), date(2025, 2, 25))

    credit = VendorCredit.objects.get(number="VC-0001")
    assert credit.kind == VendorCreditKind.RETURN
    line = credit.lines.get()
    assert (line.item.sku, line.quantity, line.amount) == ("WID-A", 10, Decimal("120.00"))
    assert line.stock_movement.quantity == -10
    app = credit.applications.get()
    assert (app.bill.vendor_invoice_number, app.amount) == ("BILL-003", Decimal("120.00"))
    assert bill_balance(app.bill) == EXPECTED["bill_totals"]["BILL-003"] - Decimal("120.00")


def test_undeposited_transfer_becomes_deposit(replay):
    deposit = Deposit.objects.get()
    assert deposit.date == date(2024, 12, 22) and deposit.bank_account.name == BANK
    assert set(deposit.payments.values_list("number", flat=True)) == {"1", "3"}
    assert sum(p.amount for p in deposit.payments.all()) == EXPECTED["undeposited_transfer"]
    assert ZohoRecord.objects.get(kind="undeposited_transfer", zoho_id="Transfer_Fund.csv:2").target == deposit
    assert not JournalEntry.objects.filter(source_type="migration.transfer", source_id="Transfer_Fund.csv:2")
    assert undeposited_check() == []


def test_undeposited_transfer_without_exact_set_warns(db, admin_user, tmp_path):
    r = _replay(admin_user, tmp_path, extra_rows=UNMATCHED)
    assert "Undeposited transfer 2025-06-30 1307.50: no exact set of payments." in r.warnings
    assert Deposit.objects.count() == 1  # only the 2024 one; nothing posted for this transfer
    assert not JournalEntry.objects.filter(source_type="migration.transfer", source_id="Transfer_Fund.csv:4")
    assert undeposited_check() == []


def test_deposit_pairs_resolve_it(db, admin_user, tmp_path):
    pairs = tmp_path / "deposit_pairs.csv"
    pairs.write_text('date,amount,payment_numbers\n2025-06-30,"1,307.50",2;4\n', encoding="utf-8")
    r = _replay(admin_user, tmp_path, extra_rows=UNMATCHED, pairs=pairs)
    assert not [w for w in r.warnings if w.startswith("Undeposited transfer")]
    deposit = Deposit.objects.get(date=date(2025, 6, 30))
    assert set(deposit.payments.values_list("number", flat=True)) == {"2", "4"}
    assert deposit.bank_account.name == BANK
    assert undeposited_check() == []


def test_expense_journal_transfer_other_deposit_entries(replay):
    assert _entry("migration.expense", "58000000001") == {"Office Supplies": Decimal("85.40"), CARD: Decimal("-85.40")}
    assert _entry("migration.expense", "58000000002") == {
        "Interest Expense": Decimal("120.00"), "Line Of Credit - Headway": Decimal("-120.00")}
    assert _entry("migration.journal", "59000000001") == {
        "Bank Fees and Charges": Decimal("15.00"), "Office Supplies": Decimal("-15.00")}
    assert _entry("migration.transfer", "Transfer_Fund.csv:1") == {CARD: Decimal("85.40"), BANK: Decimal("-85.40")}
    assert _entry("migration.transfer", "Transfer_Fund.csv:3") == {
        BANK: Decimal("2000.00"), "Line Of Credit - Chase": Decimal("-2000.00")}
    assert JournalEntry.objects.filter(source_type="migration.transfer").count() == 2
    assert _entry("migration.deposit", "Deposit.csv:1") == {BANK: Decimal("5000.00"), "Owner's Equity": Decimal("-5000.00")}
    assert _entry("migration.deposit", "Deposit.csv:2") == {BANK: Decimal("20.00"), "Office Supplies": Decimal("-20.00")}
    entry = JournalEntry.objects.get(source_type="migration.expense", source_id="58000000001")
    assert entry.date == date(2024, 5, 5) and entry.reference == "RCPT-77"
    assert ZohoRecord.objects.get(kind="journal", zoho_id="59000000001").target == JournalEntry.objects.get(
        source_type="migration.journal")


def test_quantity_and_value_adjustments(replay):
    qty = StockMovement.objects.get(kind=MovementKind.ADJUSTMENT)
    assert (qty.item.sku, qty.quantity, qty.date, qty.reason) == ("WID-A", -2, date(2025, 10, 1), "Damaged")
    assert qty.offset_account.name == "Cost of Goods Sold"
    value = StockMovement.objects.get(kind=MovementKind.VALUE_ADJUSTMENT)
    assert (value.item.sku, value.total_cost, value.reason) == ("GAD-C", Decimal("-50.00"), "Revaluation")
    assert value.offset_account.name == "Cost of Goods Sold"
    assert ZohoRecord.objects.get(kind="adjustment", zoho_id="60000000002").target == value


def test_opening_balances_csv(db, admin_user, tmp_path):
    r = Replay(read_export(build_sample_export(tmp_path)), admin_user)
    r.load_master()
    path = tmp_path / "opening_balances.csv"
    path.write_text(
        f'account,debit,credit\n{BANK},"1,000.00",0\n"Owner\'s Equity",,800.00\n', encoding="utf-8")
    r.load_opening_balances(path)
    entry = JournalEntry.objects.get(source_type="migration.opening")
    assert entry.date == date(2020, 2, 1)
    assert _entry("migration.opening", entry.source_id) == {
        BANK: Decimal("1000.00"), "Owner's Equity": Decimal("-800.00"), "Opening Balance Offset": Decimal("-200.00")}


def test_all_ledger_checks_zero_after_full_replay(db, admin_user, tmp_path):
    problems = []

    def check(event):
        found = ar_check() + ap_check() + undeposited_check() + check_invariants()
        if found:
            problems.append((event, found))

    r = _replay(admin_user, tmp_path, after_each=check)
    assert problems == []
    assert not [w for w in r.warnings if w.startswith("Undeposited transfer")]
    kinds = {e.kind for e in r.events()}
    assert kinds == {
        "sales_order", "purchase_order", "bill", "invoice", "customer_payment", "vendor_payment",
        "credit_note", "credit_application", "vendor_credit", "undeposited_transfer", "expense",
        "journal", "transfer", "other_deposit", "adjustment"}


def test_exact_subset_prefers_oldest_and_needs_exact_sum():
    from migration.zoho.replay_money import exact_subset

    amounts = [Decimal(x) for x in ("100.00", "50.00", "60.00", "40.00")]
    assert exact_subset(amounts, Decimal("100.00")) == [0]  # the oldest single payment, not 60 + 40
    assert exact_subset(amounts, Decimal("150.00")) == [0, 1]
    assert exact_subset(amounts, Decimal("101.00")) is None
    assert exact_subset([], Decimal("5.00")) is None


def test_bank_charges_on_undeposited_payment_warn_and_post_nothing(db, admin_user, tmp_path):
    row = dict(SMALL_CASH[0], **{
        "CustomerPayment ID": "54200000001", "Payment Number": "CP-3001", "Date": "2025-07-15",
        "Amount": "50.00", "Unused Amount": "50.00", "Bank Charges": "2.00"})
    r = _replay(admin_user, tmp_path, extra_rows={"customer_payments": [row]})
    assert "Bank charges on payment CP-3001: 2.00 not posted (deposited to Undeposited Funds)." in r.warnings
    assert not JournalEntry.objects.filter(source_type="migration.expense", source_id="54200000001")
    assert not ZohoRecord.objects.filter(kind="bank_charges", zoho_id="54200000001")
    assert Payment.objects.get(number="CP-3001").amount == Decimal("50.00")
    assert undeposited_check() == [] and ar_check() == [] and check_invariants() == []

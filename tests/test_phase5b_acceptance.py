"""Spec sections 6 to 10: the sample Zoho export imported, then caught up through the inbox and reconciled, with every
ledger check at zero throughout."""
from datetime import date
from decimal import Decimal

from banking import inbox, reconcile
from banking.importers import import_csv
from banking.models import BankAccount, BankTransaction, ReconStatus, Reconciliation, Rule, TxnStatus
from contacts.models import Customer, Vendor
from items.models import Item
from items.stock import check_invariants
from ledger.balances import account_totals
from ledger.models import Account
from migration.models import ImportRun
from purchasing.checks import ap_check
from purchasing.models import VendorPayment, VendorPaymentMethod
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.checks import ar_check, undeposited_check
from sales.models import Deposit, Invoice, Payment, PaymentMethod
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from tests.test_import_zoho_command import _export, _matching_reports, _run, empty_file_db  # noqa: F401
from tests.zoho_sample import BANK, CUSTOMERS, VENDORS

D = Decimal


def zero_checks():
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []
    totals = account_totals(start=None, end=date.max)
    assert sum(v[0] for v in totals.values()) == sum(v[1] for v in totals.values())


def ledger_balance(account, through=date.max):
    debit, credit = account_totals(start=None, end=through).get(account.pk, (D("0.00"), D("0.00")))
    return debit - credit


def test_import_and_catch_up(empty_file_db, tmp_path, admin_user):  # noqa: F811
    reports = _matching_reports(tmp_path, admin_user)
    export = _export(tmp_path)

    # Dry run first: exit 0 and nothing is kept. Then the real run.
    code, out, err = _run(export, "--reports", reports, "--user", "admin", "--dry-run")
    assert code == 0, out + err
    assert not ImportRun.objects.exists() and not Invoice.objects.exists()
    code, out, err = _run(export, "--reports", reports, "--user", "admin")
    assert code == 0, out + err
    assert out.strip().endswith("Imported. Restart the app.")
    zero_checks()

    # The Dec 31, 2025 reconciliation is left in progress; the owner types the statement balance and finishes it.
    bank = BankAccount.objects.get(ledger_account__name=BANK)
    dec = Reconciliation.objects.get(bank_account=bank, statement_date=date(2025, 12, 31))
    assert dec.status == ReconStatus.IN_PROGRESS and dec.statement_balance is None
    dec_balance = ledger_balance(bank.ledger_account, date(2025, 12, 31))
    reconcile.set_statement_balance(dec, dec_balance, admin_user)
    assert reconcile.difference(dec) == D("0.00")
    reconcile.finish(dec, user=admin_user)
    dec.refresh_from_db()
    assert dec.status == ReconStatus.FINISHED
    zero_checks()

    # Catch-up documents the bank rows will be matched to.
    customer, vendor = Customer.objects.get(name=CUSTOMERS[0]), Vendor.objects.get(name=VENDORS[1])
    invoice = create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user,
                             lines=[InvoiceLineSpec(Item.objects.get(sku="WRAP"), 10, D("5.00"))])
    expense = Account.objects.get(name="Office Supplies")
    bill = create_bill(vendor=vendor, date=date(2026, 1, 6), vendor_invoice_number="CATCH-1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("80.00"))])
    fees = Account.objects.get(name="Bank Fees and Charges")
    Rule.objects.create(pattern="SERVICE FEE", account=fees, memo="Monthly bank fee")
    zero_checks()

    # Statement file for January and February, one row before the feed start (Jan 1, 2026).
    bank.csv_mapping = {"date": "Date", "description": "Description", "amount": "Amount", "date_format": "%m/%d/%Y"}
    bank.save()
    result = import_csv(bank, (
        "Date,Description,Amount\n"
        "12/30/2025,OLD CHECK,-20.00\n"
        "01/10/2026,ACH PIONEER WHOLESALE,-80.00\n"
        "01/12/2026,DEPOSIT ACME CHECK 1042,50.00\n"
        "01/31/2026,SERVICE FEE,-15.00\n"
        "02/03/2026,UNKNOWN DEBIT,-7.00\n"), user=admin_user)
    assert (result.added, result.before_feed_start) == (4, 1)
    old = BankTransaction.objects.get(description="OLD CHECK")
    assert old.status == TxnStatus.EXCLUDED and "feed" in old.exclude_reason.lower()
    zero_checks()

    # Pay bills on the outgoing row.
    out_row = BankTransaction.objects.get(description="ACH PIONEER WHOLESALE")
    payment = inbox.pay_bills_from_row(out_row, vendor=vendor, amounts={bill.pk: D("80.00")}, user=admin_user)
    out_row.refresh_from_db()
    assert out_row.status == TxnStatus.MATCHED and payment.method == VendorPaymentMethod.ACH
    assert payment.paid_from == bank.ledger_account
    zero_checks()

    # Receive payment (check) on the incoming row: a payment and a one-payment deposit matched to the row.
    in_row = BankTransaction.objects.get(description="DEPOSIT ACME CHECK 1042")
    received = inbox.receive_payment_from_row(
        in_row, customer=customer, amounts={invoice.pk: D("50.00")}, method=PaymentMethod.CHECK, user=admin_user)
    in_row.refresh_from_db()
    assert in_row.status == TxnStatus.MATCHED and in_row.match.deposit.payments.get() == received
    zero_checks()

    # The fee is booked by rule.
    fee_row = BankTransaction.objects.get(description="SERVICE FEE")
    suggestion = inbox.suggest(fee_row)
    assert suggestion.kind == "rule"
    before = ledger_balance(fees)
    inbox.confirm(fee_row, suggestion, user=admin_user)
    fee_row.refresh_from_db()
    assert fee_row.status == TxnStatus.BOOKED and ledger_balance(fees) == before + D("15.00")
    zero_checks()

    # The February row stays in the inbox; the pre-feed row never shows there.
    waiting = BankTransaction.objects.filter(status=TxnStatus.NEW)
    assert [t.description for t in waiting] == ["UNKNOWN DEBIT"]

    # January 2026 reconciles at 0.00.
    jan_balance = ledger_balance(bank.ledger_account, date(2026, 1, 31))
    rec = reconcile.start(bank, statement_date=date(2026, 1, 31), statement_balance=jan_balance, user=admin_user)
    for item in rec.items.all():
        reconcile.toggle(item, True)
    assert reconcile.difference(rec) == D("0.00")
    reconcile.finish(rec, user=admin_user)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED
    zero_checks()
    assert Payment.objects.filter(pk=received.pk).exists() and Deposit.objects.exists()
    assert VendorPayment.objects.filter(pk=payment.pk).exists()


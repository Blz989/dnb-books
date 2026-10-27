from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from banking import inbox, reconcile
from banking.models import (
    AccountKind, BankAccount, BankTransaction, ReconStatus, TransferKind, TxnSource,
)
from banking.reconcile import ReconcileError
from banking.transfers import create_transfer, void_transfer
from ledger.errors import EntryStateError
from ledger.models import Account, AccountSubtype, AccountType
from ledger.posting import LineSpec, post_entry, void_entry
from purchasing.errors import PurchasingError
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.bills import BillExpenseSpec, create_bill, edit_bill
from purchasing.posting.payments import pay_vendor, void_vendor_payment
from sales.models import PaymentMethod, RefundMethod
from sales.posting.deposits import make_deposit, void_deposit
from sales.posting.receipts import issue_refund, receive_payment, void_payment, void_refund

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def jan(setup, admin_user, bank, customer, vendor):
    """Checking in January: +1000 customer ACH, -300 check to Acme."""
    pay = receive_payment(customer=customer, date=d(1, 3), method=PaymentMethod.ACH, amount=D("1000.00"),
                          user=admin_user, bank_account=bank)
    check = pay_vendor(vendor=vendor, date=d(1, 10), method=VM.CHECK, amount=D("300.00"), paid_from=bank,
                       user=admin_user, check_number="2050")
    return pay, check


def tick(rec, *entries):
    wanted = {e.pk for e in entries}
    for item in rec.items.select_related("journal_line"):
        reconcile.toggle(item, item.journal_line.entry_id in wanted)


def clear_all(rec):
    for item in rec.items.all():
        reconcile.toggle(item, True)


def start(checking, on, balance, user):
    return reconcile.start(checking, statement_date=on, statement_balance=D(balance), user=user)


def test_difference_for_checking(jan, checking, admin_user):
    pay, check = jan
    rec = start(checking, d(1, 31), "700.00", admin_user)
    assert rec.items.count() == 2 and reconcile.difference(rec) == D("700.00")
    tick(rec, pay.journal_entry)
    assert reconcile.difference(rec) == D("-300.00")
    tick(rec, pay.journal_entry, check.journal_entry)
    assert reconcile.difference(rec) == D("0.00")


def test_card_balance_typed_as_amount_owed_is_positive(setup, admin_user, card, card_account, vendor):
    a = pay_vendor(vendor=vendor, date=d(1, 4), method=VM.CARD, amount=D("250.00"), paid_from=card, user=admin_user)
    b = pay_vendor(vendor=vendor, date=d(1, 9), method=VM.CARD, amount=D("1000.00"), paid_from=card, user=admin_user)
    rec = start(card_account, d(1, 31), "1250.00", admin_user)
    assert reconcile.difference(rec) == D("1250.00")
    tick(rec, a.journal_entry)
    assert reconcile.difference(rec) == D("1000.00")
    tick(rec, a.journal_entry, b.journal_entry)
    assert reconcile.difference(rec) == D("0.00")


def test_card_payment_to_the_card_reduces_the_amount_owed(setup, admin_user, card, card_account, checking, vendor):
    charge = pay_vendor(vendor=vendor, date=d(1, 4), method=VM.CARD, amount=D("1250.00"), paid_from=card,
                        user=admin_user)
    payoff = create_transfer(date=d(1, 20), from_account=checking, to_account=card_account, amount=D("500.00"),
                             kind=TransferKind.CARD_PAYOFF, user=admin_user)
    rec = start(card_account, d(1, 31), "750.00", admin_user)
    tick(rec, charge.journal_entry, payoff.journal_entry)
    assert reconcile.difference(rec) == D("0.00")


def test_loan_balance_is_amount_owed(setup, admin_user, loan, checking, bank, interest_expense):
    draw = post_entry(date=d(1, 2), user=admin_user, lines=[
        LineSpec(bank, debit=D("5000.00")), LineSpec(loan.ledger_account, credit=D("5000.00"))])
    pay = create_transfer(date=d(1, 15), from_account=checking, to_account=loan, amount=D("400.00"),
                          kind=TransferKind.LOAN_PAYMENT, principal=D("340.00"), interest=D("60.00"),
                          interest_account=interest_expense, user=admin_user)
    rec = start(loan, d(1, 31), "4660.00", admin_user)
    tick(rec, draw, pay.journal_entry)
    assert reconcile.difference(rec) == D("0.00")


def test_lines_after_the_statement_date_are_not_listed(jan, checking, admin_user):
    rec = start(checking, d(1, 5), "1000.00", admin_user)
    assert rec.items.count() == 1


def test_matched_lines_are_pre_ticked_only_when_the_bank_row_is_on_or_before_the_statement_date(
        jan, checking, admin_user):
    pay, check = jan
    on_time = BankTransaction.objects.create(
        bank_account=checking, external_id="a", date=d(1, 4), description="x", amount=D("1000.00"),
        source=TxnSource.FILE)
    late = BankTransaction.objects.create(
        bank_account=checking, external_id="b", date=d(2, 2), description="x", amount=D("-300.00"), check_number="2050",
        source=TxnSource.FILE)
    inbox.confirm(on_time, inbox.suggest(on_time), user=admin_user)
    inbox.confirm(late, inbox.suggest(late), user=admin_user)
    rec = start(checking, d(1, 31), "700.00", admin_user)
    cleared = {i.journal_line.entry_id for i in rec.items.select_related("journal_line") if i.cleared}
    assert cleared == {pay.journal_entry_id}


def test_only_one_reconciliation_in_progress_per_account(jan, checking, card_account, admin_user):
    start(checking, d(1, 31), "700.00", admin_user)
    with pytest.raises(ReconcileError, match="Finish or cancel the reconciliation already in progress."):
        start(checking, d(2, 28), "0.00", admin_user)
    start(card_account, d(1, 31), "0.00", admin_user)  # another account is fine


def test_finish_only_at_zero(jan, checking, admin_user):
    pay, check = jan
    rec = start(checking, d(1, 31), "700.00", admin_user)
    tick(rec, pay.journal_entry)
    with pytest.raises(ReconcileError, match="The difference must be 0.00 to finish."):
        reconcile.finish(rec, user=admin_user)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.IN_PROGRESS
    tick(rec, pay.journal_entry, check.journal_entry)
    reconcile.finish(rec, user=admin_user)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED and rec.finished_by == admin_user and rec.finished_at
    assert rec.report_pdf.read(4) == b"%PDF"


def test_finish_is_admin_only(jan, checking, admin_user, standard_user):
    rec = start(checking, d(1, 31), "700.00", standard_user)
    clear_all(rec)
    assert reconcile.difference(rec) == D("0.00")
    with pytest.raises(PermissionDenied):
        reconcile.finish(rec, user=standard_user)


def test_opening_balance_carries_and_uncleared_items_stay_on_the_list(jan, checking, admin_user, bank, vendor):
    pay, check = jan
    rec = start(checking, d(1, 31), "1000.00", admin_user)
    tick(rec, pay.journal_entry)  # the 300 check is still outstanding
    reconcile.finish(rec, user=admin_user)

    later = pay_vendor(vendor=vendor, date=d(2, 3), method=VM.ACH, amount=D("100.00"), paid_from=bank, user=admin_user)
    feb = start(checking, d(2, 28), "600.00", admin_user)
    assert {i.journal_line.entry_id for i in feb.items.select_related("journal_line")} == {
        check.journal_entry_id, later.journal_entry_id}
    assert reconcile.difference(feb) == D("-400.00")  # statement 600 - (opening 1000 + 0 cleared)
    clear_all(feb)
    assert reconcile.difference(feb) == D("0.00")
    reconcile.finish(feb, user=admin_user)


def finished_january(jan, checking, admin_user):
    rec = start(checking, d(1, 31), "700.00", admin_user)
    clear_all(rec)
    reconcile.finish(rec, user=admin_user)
    return rec


MSG = r"is part of the January 2026 reconciliation for Cash\. An admin must undo that reconciliation first\."


def test_locks_block_voiding_a_sales_payment_and_a_vendor_payment(jan, checking, admin_user):
    pay, check = jan
    finished_january(jan, checking, admin_user)
    with pytest.raises(EntryStateError, match=rf"{pay.number} {MSG}"):
        void_payment(pay, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match=rf"{check.number} {MSG}"):
        void_vendor_payment(check, user=admin_user, reason="x")


def test_locks_block_a_deposit_a_refund_a_transfer_and_an_inbox_entry(
        setup, admin_user, bank, checking, customer, expense):
    cheque = receive_payment(customer=customer, date=d(1, 2), method=PaymentMethod.CHECK, amount=D("400.00"),
                             user=admin_user)
    dep = make_deposit(payments=[cheque], bank_account=bank, date=d(1, 3), user=admin_user)
    other = receive_payment(customer=customer, date=d(1, 4), method=PaymentMethod.ACH, amount=D("100.00"),
                            user=admin_user, bank_account=bank)
    refund = issue_refund(source=other, date=d(1, 6), bank_account=bank, method=RefundMethod.ACH,
                          amount=D("25.00"), user=admin_user)
    savings_ledger = Account.objects.create(
        number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    savings = BankAccount.objects.create(ledger_account=savings_ledger, kind=AccountKind.SAVINGS)
    transfer = create_transfer(date=d(1, 7), from_account=checking, to_account=savings, amount=D("50.00"),
                               kind=TransferKind.TRANSFER, user=admin_user)
    txn = BankTransaction.objects.create(
        bank_account=checking, external_id="fee", date=d(1, 8), description="fee", amount=D("-15.00"),
        source=TxnSource.FILE)
    entry = inbox.new_entry(txn, account=expense, user=admin_user).journal_entry

    rec = start(checking, d(1, 31), "410.00", admin_user)
    clear_all(rec)
    assert reconcile.difference(rec) == D("0.00")  # 400 + 100 - 25 - 50 - 15
    reconcile.finish(rec, user=admin_user)

    with pytest.raises(EntryStateError, match=rf"{dep.number} {MSG}"):
        void_deposit(dep, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match=rf"{refund.number} {MSG}"):
        void_refund(refund, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match=rf"TR-{transfer.pk} {MSG}"):
        void_transfer(transfer, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match=rf"BT-{txn.pk} {MSG}"):
        void_entry(entry, user=admin_user, reason="x")


def test_entry_without_a_source_id_is_named_by_its_journal_number(setup, admin_user, checking, bank, equity):
    manual = post_entry(date=d(1, 2), user=admin_user, lines=[
        LineSpec(bank, debit=D("10.00")), LineSpec(equity, credit=D("10.00"))])
    rec = start(checking, d(1, 31), "10.00", admin_user)
    clear_all(rec)
    reconcile.finish(rec, user=admin_user)
    with pytest.raises(EntryStateError, match=rf"{manual.display_number} {MSG}"):
        void_entry(manual, user=admin_user, reason="x")


def test_edit_bill_is_refused_when_an_applied_payment_is_locked(setup, admin_user, bank, checking, vendor, expense):
    bill = create_bill(vendor=vendor, date=d(1, 2), vendor_invoice_number="V1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("300.00"))])
    pay = pay_vendor(vendor=vendor, date=d(1, 10), method=VM.ACH, amount=D("300.00"), paid_from=bank,
                     user=admin_user, applications=[(bill, D("300.00"))])
    rec = start(checking, d(1, 31), "-300.00", admin_user)
    clear_all(rec)
    reconcile.finish(rec, user=admin_user)
    with pytest.raises(PurchasingError, match=rf"{pay.number} {MSG}"):
        edit_bill(bill, user=admin_user, date=d(1, 2), vendor_invoice_number="V1", item_lines=[],
                  expense_lines=[BillExpenseSpec(expense, D("300.00"), "edited")])


def test_undo_unlocks_the_latest_and_refuses_an_older_one(jan, checking, admin_user, bank, vendor):
    pay, check = jan
    first = finished_january(jan, checking, admin_user)
    extra = pay_vendor(vendor=vendor, date=d(2, 3), method=VM.ACH, amount=D("100.00"), paid_from=bank, user=admin_user)
    second = start(checking, d(2, 28), "600.00", admin_user)
    clear_all(second)
    reconcile.finish(second, user=admin_user)

    with pytest.raises(ReconcileError, match="Only the most recent reconciliation can be undone."):
        reconcile.undo(first, user=admin_user, reason="x")
    with pytest.raises(ReconcileError, match="Enter a reason."):
        reconcile.undo(second, user=admin_user, reason=" ")
    with pytest.raises(EntryStateError):
        void_vendor_payment(extra, user=admin_user, reason="x")
    reconcile.undo(second, user=admin_user, reason="Wrong balance")
    second.refresh_from_db()
    assert (second.status, second.undo_reason, second.undone_by) == (ReconStatus.UNDONE, "Wrong balance", admin_user)
    void_vendor_payment(extra, user=admin_user, reason="now allowed")
    assert reconcile.reconciled_lines().filter(entry=pay.journal_entry).exists()


def test_undo_is_admin_only(jan, checking, admin_user, standard_user):
    rec = finished_january(jan, checking, admin_user)
    with pytest.raises(PermissionDenied):
        reconcile.undo(rec, user=standard_user, reason="x")


def test_cancel_deletes_an_in_progress_reconciliation(jan, checking, admin_user):
    rec = start(checking, d(1, 31), "700.00", admin_user)
    reconcile.cancel(rec, user=admin_user)
    assert not type(rec).objects.filter(pk=rec.pk).exists()
    done = finished_january(jan, checking, admin_user)
    with pytest.raises(ReconcileError, match="Only a reconciliation in progress can be cancelled."):
        reconcile.cancel(done, user=admin_user)


def test_toggle_is_refused_after_finishing(jan, checking, admin_user):
    rec = finished_january(jan, checking, admin_user)
    with pytest.raises(ReconcileError, match="This reconciliation is no longer in progress."):
        reconcile.toggle(rec.items.first(), False)


def test_report_lists_outstanding_checks_and_deposits_in_transit(jan, checking, admin_user, bank, customer, vendor):
    pay, check = jan
    transit = receive_payment(customer=customer, date=d(1, 30), method=PaymentMethod.ACH, amount=D("50.00"),
                              user=admin_user, bank_account=bank)
    rec = start(checking, d(1, 31), "1000.00", admin_user)
    tick(rec, pay.journal_entry)
    reconcile.toggle(rec.items.get(journal_line__entry=pay.journal_entry), True)
    assert reconcile.difference(rec) == D("0.00")
    html = reconcile.render_report_html(rec)
    assert "Outstanding checks and payments" in html and check.number in html and "300.00" in html
    assert "Deposits in transit" in html and transit.number in html
    assert "Statement balance" in html and "1,000.00" in html
    reconcile.finish(rec, user=admin_user)
    rec.refresh_from_db()
    assert rec.report_pdf.read(4) == b"%PDF"


def test_reconciled_lines_only_counts_cleared_lines_of_finished_reconciliations(jan, checking, admin_user):
    pay, check = jan
    rec = start(checking, d(1, 31), "700.00", admin_user)
    clear_all(rec)
    assert reconcile.reconciled_lines().count() == 0
    reconcile.finish(rec, user=admin_user)
    assert reconcile.reconciled_lines().count() == 2

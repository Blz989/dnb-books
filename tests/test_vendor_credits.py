from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from items.errors import NegativeStockWarning
from items.models import ItemType
from items.stock import check_invariants, on_hand
from ledger.balances import account_totals, money
from ledger.models import Account, JournalEntry
from purchasing.balances import bill_balance, vendor_balance, vendor_credit_unapplied
from purchasing.errors import PurchasingError
from purchasing.models import VendorCredit, VendorCreditKind
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill
from purchasing.posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, apply_vendor_credit, create_allowance_credit, create_return_credit,
    void_vendor_credit,
)
from purchasing.posting.payments import remove_bill_application
from sales.models import DocStatus

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


def net(account):
    debit, credit = account_totals(start=None, end=date.max).get(account.pk, (D("0.00"), D("0.00")))
    return money(debit - credit)


@pytest.fixture
def setup(inv_settings, purchasing_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.4800")


@pytest.fixture
def stock_bill(vendor, admin_user, widget):
    return create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="V-1", user=admin_user,
                       item_lines=[BillItemSpec(widget, 10, D("0.48"))])


def test_return_splits_engine_cost_from_credit_amount(stock_bill, vendor, widget, admin_user, ap, inventory_account, cogs_account):
    credit = create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="Wrong size",
                                  lines=[ReturnLineSpec(widget, 10, D("5.00"))])
    assert credit.number == "VC-00001" and credit.kind == VendorCreditKind.RETURN and credit.status == DocStatus.OPEN
    (line,) = credit.lines.all()
    assert (line.item, line.quantity, line.amount) == (widget, 10, D("5.00"))
    movement = line.stock_movement
    assert (movement.quantity, movement.total_cost) == (-10, D("-4.80"))
    assert (movement.source_type, movement.source_id) == ("purchasing.vendor_credit", "VC-00001")
    assert entry_lines(movement.journal_entry) == [("1300", D("0"), D("4.80")), ("5000", D("4.80"), D("0"))]
    assert entry_lines(credit.journal_entry) == [("2000", D("5.00"), D("0")), ("5000", D("0"), D("5.00"))]
    assert (credit.journal_entry.source_type, credit.journal_entry.source_id) == ("purchasing.vendor_credit", "VC-00001")
    assert (net(inventory_account), net(ap), net(cogs_account)) == (D("0.00"), D("0.20"), D("-0.20"))
    assert on_hand(widget) == 0
    assert vendor_credit_unapplied(credit) == D("5.00")
    assert check_invariants() == []


def test_default_amount_is_quantity_times_current_cost(stock_bill, vendor, widget, admin_user):
    credit = create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                                  lines=[ReturnLineSpec(widget, 3)])
    assert credit.lines.get().amount == D("1.44")
    assert entry_lines(credit.journal_entry) == [("2000", D("1.44"), D("0")), ("5000", D("0"), D("1.44"))]


def test_return_warns_before_taking_stock_negative(stock_bill, vendor, widget, admin_user):
    with pytest.raises(NegativeStockWarning):
        create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                             lines=[ReturnLineSpec(widget, 12, D("5.00"))])
    assert VendorCredit.objects.count() == 0 and on_hand(widget) == 10
    create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                         lines=[ReturnLineSpec(widget, 12, D("5.00"))], allow_negative=True)
    assert on_hand(widget) == -2


def test_return_validation(stock_bill, vendor, widget, admin_user, make_item):
    service = make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)
    with pytest.raises(PurchasingError, match="Add at least one line."):
        create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="", lines=[])
    with pytest.raises(PurchasingError, match="Line 1: SVC is not an active stock item."):
        create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                             lines=[ReturnLineSpec(service, 1, D("1.00"))])
    with pytest.raises(PurchasingError, match="Line 1: enter a quantity above zero."):
        create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                             lines=[ReturnLineSpec(widget, 0, D("1.00"))])
    with pytest.raises(PurchasingError, match="Line 1: enter an amount above zero."):
        create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                             lines=[ReturnLineSpec(widget, 1, D("0.00"))])
    assert VendorCredit.objects.count() == 0


def test_allowance_posts_to_the_vendors_default_account(vendor, admin_user, expense, ap, setup):
    with pytest.raises(PurchasingError, match="Line 1: choose an account."):
        create_allowance_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="Late",
                                lines=[AllowanceLineSpec(None, D("25.00"))])
    vendor.default_expense_account = expense
    vendor.save()
    credit = create_allowance_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="Late",
                                     lines=[AllowanceLineSpec(None, D("25.00"), "Late delivery")])
    assert credit.kind == VendorCreditKind.ALLOWANCE
    assert entry_lines(credit.journal_entry) == [("2000", D("25.00"), D("0")), ("6000", D("0"), D("25.00"))]
    assert net(ap) == D("25.00")  # net debit: no bill to offset it


def test_apply_credit_to_a_bill(stock_bill, vendor, admin_user, expense):
    credit = create_allowance_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                                     lines=[AllowanceLineSpec(expense, D("3.00"))])
    with pytest.raises(PurchasingError, match="This credit has 3.00 left to apply."):
        apply_vendor_credit(credit, stock_bill, D("3.01"), date=d(10, 11), user=admin_user)
    with pytest.raises(PurchasingError, match=f"The date can't be before {credit.number}'s date."):
        apply_vendor_credit(credit, stock_bill, D("1.00"), date=d(10, 9), user=admin_user)
    apply_vendor_credit(credit, stock_bill, D("2.00"), date=d(10, 11), user=admin_user)
    assert bill_balance(stock_bill) == D("2.80")
    assert vendor_credit_unapplied(credit) == D("1.00")
    assert vendor_balance(vendor) == D("1.80")
    big = create_allowance_credit(vendor=vendor, date=d(10, 12), user=admin_user, reason="",
                                  lines=[AllowanceLineSpec(expense, D("50.00"))])
    with pytest.raises(PurchasingError, match=f"{stock_bill.number} has 2.80 left to pay."):
        apply_vendor_credit(big, stock_bill, D("3.00"), date=d(10, 12), user=admin_user)


def test_void_return_restores_stock(stock_bill, vendor, widget, admin_user, ap):
    credit = create_return_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                                  lines=[ReturnLineSpec(widget, 10, D("5.00"))])
    void_vendor_credit(credit, user=admin_user, reason="Vendor refused it")
    assert credit.status == DocStatus.VOID and credit.voided_on == d(10, 10)
    assert on_hand(widget) == 10
    assert net(ap) == D("-4.80")
    assert JournalEntry.objects.filter(reverses=credit.journal_entry).exists()
    assert check_invariants() == []


def test_void_blocked_by_applications(stock_bill, vendor, admin_user, expense):
    credit = create_allowance_credit(vendor=vendor, date=d(10, 10), user=admin_user, reason="",
                                     lines=[AllowanceLineSpec(expense, D("3.00"))])
    app = apply_vendor_credit(credit, stock_bill, D("2.00"), date=d(10, 11), user=admin_user)
    with pytest.raises(PurchasingError, match="VC-00001 is applied to bills. Remove the applications first."):
        void_vendor_credit(credit, user=admin_user, reason="x")
    remove_bill_application(app, date=d(10, 12), user=admin_user)
    void_vendor_credit(credit, user=admin_user, reason="x")
    assert credit.voided_on == d(10, 12)
    assert bill_balance(stock_bill) == D("4.80")


def test_standard_user_can_create_but_not_void(stock_bill, vendor, standard_user, expense):
    credit = create_allowance_credit(vendor=vendor, date=d(10, 10), user=standard_user, reason="",
                                     lines=[AllowanceLineSpec(expense, D("3.00"))])
    with pytest.raises(PermissionDenied):
        void_vendor_credit(credit, user=standard_user, reason="x")

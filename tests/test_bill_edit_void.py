from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from contacts.models import Vendor
from items.errors import NegativeStockWarning
from items.models import StockMovement
from items.stock import check_invariants, effective_total, on_hand
from ledger.balances import account_totals, money
from ledger.errors import PeriodClosedError
from ledger.models import JournalEntry
from ledger.periods import close_period
from purchasing.balances import bill_total
from purchasing.errors import PurchasingError
from purchasing.models import BillPaymentApplication, DropShipAllocation, VendorPayment, VendorPaymentMethod
from purchasing.posting.bills import (
    BillExpenseSpec, BillItemSpec, bill_from_po, create_bill, edit_bill, void_bill,
)
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import DocStatus, Fulfillment, OrderStatus
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


def ap_balance(ap):
    debit, credit = account_totals(start=None, end=date.max).get(ap.pk, (D("0.00"), D("0.00")))
    return money(credit - debit)


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.5000", sale_price="2.0000")


@pytest.fixture
def stock_bill(vendor, admin_user, widget, freight):
    return create_bill(
        vendor=vendor, date=d(10, 5), vendor_invoice_number="V-1", user=admin_user,
        item_lines=[BillItemSpec(widget, 10, D("0.50"))],
        expense_lines=[BillExpenseSpec(freight, D("25.00"), "Freight")])


def edit(bill, admin_user, widget, freight, qty=10, price=D("0.50"), freight_amount=D("25.00"), **kwargs):
    args = dict(
        user=admin_user, date=bill.date, vendor_invoice_number=bill.vendor_invoice_number,
        item_lines=[BillItemSpec(widget, qty, price)], expense_lines=[BillExpenseSpec(freight, freight_amount)])
    args.update(kwargs)
    return edit_bill(bill, **args)


def sell(customer, admin_user, widget, qty, day=10):
    return create_invoice(customer=customer, date=d(10, day), user=admin_user,
                          lines=[InvoiceLineSpec(widget, qty, D("2.00"))])


def test_edit_saves_revision_and_reposts(stock_bill, admin_user, widget, freight, ap):
    old_entry = stock_bill.journal_entry
    bill = edit(stock_bill, admin_user, widget, freight, freight_amount=D("40.00"), reason="Freight was wrong")
    assert bill.revision == 2 and bill.status == DocStatus.OPEN
    (revision,) = bill.revisions.all()
    assert revision.revision == 1 and revision.reason == "Freight was wrong" and revision.edited_by == admin_user
    snap = revision.snapshot
    assert snap["vendor_invoice_number"] == "V-1" and snap["date"] == "2026-10-05"
    assert snap["expense_lines"][0]["amount"] == "25.00"
    assert snap["item_lines"][0]["sku"] == "WIDGET" and snap["item_lines"][0]["quantity"] == 10
    assert bill.journal_entry != old_entry
    old_entry.refresh_from_db()
    assert JournalEntry.objects.filter(reverses=old_entry).exists()
    assert bill_total(bill) == D("45.00")
    assert ap_balance(ap) == D("45.00")
    assert on_hand(widget) == 10 and check_invariants() == []


def test_editing_a_stock_price_recosts_a_later_sale(stock_bill, admin_user, widget, freight, customer, ap):
    sale = sell(customer, admin_user, widget, 4)
    movement = sale.lines.get().stock_movement
    assert effective_total(movement) == D("-2.00")
    edit(stock_bill, admin_user, widget, freight, price=D("0.60"))
    movement.refresh_from_db()
    assert effective_total(movement) == D("-2.40")
    assert check_invariants() == []
    assert ap_balance(ap) == D("31.00")  # 10 x 0.60 + 25.00 freight


def test_edit_that_drops_stock_below_whats_sold_warns_then_saves(stock_bill, admin_user, widget, freight, customer):
    sell(customer, admin_user, widget, 8)
    with pytest.raises(NegativeStockWarning):
        edit(stock_bill, admin_user, widget, freight, qty=5)
    stock_bill.refresh_from_db()
    assert stock_bill.revision == 1 and stock_bill.revisions.count() == 0
    assert on_hand(widget) == 2
    edit(stock_bill, admin_user, widget, freight, qty=5, allow_negative=True)
    assert on_hand(widget) == -3


def test_edit_below_applied_amount_is_refused(stock_bill, vendor, admin_user, widget, freight, cash):
    payment = VendorPayment.objects.create(
        number="VP-00001", vendor=vendor, date=d(10, 6), method=VendorPaymentMethod.ACH, paid_from=cash,
        amount=D("30.00"), created_by=admin_user)
    BillPaymentApplication.objects.create(payment=payment, bill=stock_bill, amount=D("30.00"), applied_on=d(10, 6))
    with pytest.raises(PurchasingError, match=r"This bill has 30.00 applied. The new total can't be less than that."):
        edit(stock_bill, admin_user, widget, freight, qty=1, price=D("0.50"), freight_amount=D("5.00"))
    with pytest.raises(PurchasingError, match="The vendor can't change after payments or credits are applied."):
        edit(stock_bill, admin_user, widget, freight, vendor=Vendor.objects.create(name="Other Co"))


def test_edit_date_must_not_follow_an_application(stock_bill, vendor, admin_user, widget, freight, cash):
    payment = VendorPayment.objects.create(
        number="VP-00001", vendor=vendor, date=d(10, 6), method=VendorPaymentMethod.ACH, paid_from=cash,
        amount=D("30.00"), created_by=admin_user)
    BillPaymentApplication.objects.create(payment=payment, bill=stock_bill, amount=D("30.00"), applied_on=d(10, 6))
    with pytest.raises(PurchasingError, match="The bill date can't be after 2026-10-06"):
        edit(stock_bill, admin_user, widget, freight, date=d(10, 7))


def test_edit_into_a_closed_month_is_refused(stock_bill, admin_user, widget, freight):
    close_period(2026, 9, user=admin_user)
    with pytest.raises(PeriodClosedError):
        edit(stock_bill, admin_user, widget, freight, date=d(9, 20))
    stock_bill.refresh_from_db()
    assert stock_bill.revision == 1


def test_edit_can_change_vendor_without_applications(stock_bill, admin_user, widget, freight):
    other = Vendor.objects.create(name="Other Co")
    bill = edit(stock_bill, admin_user, widget, freight, vendor=other)
    assert bill.vendor == other


def test_void_bill_restores_stock_and_ap(stock_bill, admin_user, ap):
    void_bill(stock_bill, user=admin_user, reason="Entered twice")
    assert stock_bill.status == DocStatus.VOID and stock_bill.voided_on == d(10, 5)
    assert stock_bill.void_reason == "Entered twice"
    assert ap_balance(ap) == D("0.00")
    assert check_invariants() == []
    assert StockMovement.objects.filter(voided=False).count() == 0


def test_void_bill_whose_stock_was_sold_warns_then_succeeds(stock_bill, admin_user, widget, customer):
    sell(customer, admin_user, widget, 8)
    with pytest.raises(NegativeStockWarning):
        void_bill(stock_bill, user=admin_user, reason="Wrong")
    stock_bill.refresh_from_db()
    assert stock_bill.status == DocStatus.OPEN
    void_bill(stock_bill, user=admin_user, reason="Wrong", allow_negative=True)
    assert on_hand(widget) == -8
    assert check_invariants() == []


def test_void_with_applications_is_refused(stock_bill, vendor, admin_user, cash):
    payment = VendorPayment.objects.create(
        number="VP-00001", vendor=vendor, date=d(10, 6), method=VendorPaymentMethod.ACH, paid_from=cash,
        amount=D("30.00"), created_by=admin_user)
    app = BillPaymentApplication.objects.create(payment=payment, bill=stock_bill, amount=D("30.00"), applied_on=d(10, 6))
    with pytest.raises(PurchasingError, match="BILL-00001 has payments or credits applied. Remove them first."):
        void_bill(stock_bill, user=admin_user, reason="x")
    app.removed, app.removed_on = True, d(10, 8)
    app.save()
    void_bill(stock_bill, user=admin_user, reason="x")
    assert stock_bill.voided_on == d(10, 8)


def test_void_needs_a_reason_and_cannot_repeat(stock_bill, admin_user):
    with pytest.raises(PurchasingError, match="Enter a reason."):
        void_bill(stock_bill, user=admin_user, reason="  ")
    void_bill(stock_bill, user=admin_user, reason="x")
    with pytest.raises(PurchasingError, match="BILL-00001 is already void."):
        void_bill(stock_bill, user=admin_user, reason="x")


def test_void_bill_cannot_be_edited(stock_bill, admin_user, widget, freight):
    void_bill(stock_bill, user=admin_user, reason="x")
    with pytest.raises(PurchasingError, match="BILL-00001 is void and can't be edited."):
        edit(stock_bill, admin_user, widget, freight)


def test_standard_user_can_edit_but_not_void(stock_bill, standard_user, widget, freight):
    edit(stock_bill, standard_user, widget, freight, freight_amount=D("30.00"))
    with pytest.raises(PermissionDenied):
        void_bill(stock_bill, user=standard_user, reason="x")


def test_void_reopens_the_purchase_order(vendor, admin_user, widget):
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 100, D("0.50"))])
    bill = bill_from_po(po, date=d(10, 5), vendor_invoice_number="V", quantities={po.lines.get().pk: 100},
                        user=admin_user)
    po.refresh_from_db()
    assert po.status == OrderStatus.CLOSED
    void_bill(bill, user=admin_user, reason="x")
    po.refresh_from_db()
    assert po.status == OrderStatus.OPEN


def test_editing_a_po_bill_can_use_the_whole_remaining_quantity(vendor, admin_user, widget):
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 100, D("0.50"))])
    line = po.lines.get()
    bill = bill_from_po(po, date=d(10, 5), vendor_invoice_number="V", quantities={line.pk: 100}, user=admin_user)
    edit_bill(bill, user=admin_user, date=d(10, 5), vendor_invoice_number="V",
              item_lines=[BillItemSpec(widget, 100, D("0.55"), po_line=line)], expense_lines=[])
    assert bill_total(bill) == D("55.00")
    with pytest.raises(PurchasingError, match=r"Line 1: only 100 left to receive on PO-00001\."):
        edit_bill(bill, user=admin_user, date=d(10, 5), vendor_invoice_number="V",
                  item_lines=[BillItemSpec(widget, 101, D("0.55"), po_line=line)], expense_lines=[])


@pytest.fixture
def drop_ship(customer, admin_user, widget, vendor):
    vendor.name = "Acme Mfg"
    vendor.save()
    order = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(widget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])
    (po,) = create_drop_ship_pos(order, user=admin_user, date=d(10, 2))
    line = order.lines.get()
    invoice_from_order(order, date=d(10, 3), quantities={line.pk: 3}, user=admin_user)
    bill = bill_from_po(po, date=d(10, 4), vendor_invoice_number="DS", quantities={po.lines.get().pk: 3},
                        user=admin_user)
    return order, po, bill


def test_edit_and_void_rerun_drop_ship_allocations(drop_ship, admin_user, widget):
    order, po, bill = drop_ship
    invoice_line = order.invoices.get().lines.get()
    invoice_line.refresh_from_db()
    assert invoice_line.drop_ship_billed is True
    edit_bill(bill, user=admin_user, date=d(10, 4), vendor_invoice_number="DS", expense_lines=[],
              item_lines=[BillItemSpec(widget, 2, D("4.00"), po_line=po.lines.get())])
    invoice_line.refresh_from_db()
    assert invoice_line.drop_ship_billed is False
    assert DropShipAllocation.objects.get().quantity == 2
    void_bill(bill, user=admin_user, reason="x")
    invoice_line.refresh_from_db()
    assert invoice_line.drop_ship_billed is False
    assert DropShipAllocation.objects.count() == 0

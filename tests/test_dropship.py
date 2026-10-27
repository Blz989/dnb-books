from datetime import date
from decimal import Decimal

import pytest

from items.models import ItemType
from items.stock import on_hand
from purchasing.models import BillLineKind, DropShipAllocation
from purchasing.posting.bills import BillItemSpec, bill_from_po
from purchasing.posting.orders import create_drop_ship_pos
from sales.models import Fulfillment
from sales.posting.invoices import InvoiceLineSpec, edit_invoice, invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order
from sales.reports import drop_ship_awaiting_bill
from contacts.models import Vendor

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")


@pytest.fixture
def order(customer, admin_user, gadget):
    return create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(gadget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])


@pytest.fixture
def po(order, admin_user):
    (po,) = create_drop_ship_pos(order, user=admin_user, date=d(10, 2))
    return po


def invoice(order, qty, admin_user, day):
    line = order.lines.get()
    return invoice_from_order(order, date=d(10, day), quantities={line.pk: qty}, user=admin_user)


def bill(po, qty, admin_user, suffix="1", price=None):
    line = po.lines.get()
    return bill_from_po(po, date=d(10, 8), vendor_invoice_number=f"V-{suffix}", quantities={line.pk: qty},
                        prices={line.pk: price} if price else None, user=admin_user)


def billed_flags(order):
    return [l.drop_ship_billed for l in sorted(
        (l for i in order.invoices.all() for l in i.lines.all()), key=lambda l: l.invoice.number)]


def test_drop_ship_bill_posts_cogs_no_stock(po, gadget, admin_user):
    b = bill(po, 3, admin_user, price=D("4.10"))
    (line,) = b.item_lines.all()
    assert line.kind == BillLineKind.DROP_SHIP and line.stock_movement is None
    assert entry_lines(b.journal_entry) == [("2000", D("0"), D("12.30")), ("5000", D("12.30"), D("0"))]
    assert on_hand(gadget) == 0


def test_non_stock_drop_ship_item_uses_default_cogs(customer, admin_user, setup, make_item):
    svc = make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                    purchase_cost="12.0000")
    order = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(svc, 1, D("40.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])
    (po,) = create_drop_ship_pos(order, user=admin_user, date=d(10, 2))
    b = bill(po, 1, admin_user)
    assert entry_lines(b.journal_entry) == [("2000", D("0"), D("12.00")), ("5000", D("12.00"), D("0"))]


def test_allocation_goes_to_oldest_invoice_first(order, po, admin_user):
    first = invoice(order, 1, admin_user, 3)
    second = invoice(order, 2, admin_user, 4)
    bill(po, 2, admin_user)
    assert [(a.invoice_line.invoice.number, a.quantity) for a in DropShipAllocation.objects.order_by("id")] == [
        (first.number, 1), (second.number, 1)]
    assert billed_flags(order) == [True, False]
    assert [l.invoice.number for l in drop_ship_awaiting_bill()] == [second.number]


def test_bill_before_invoice_is_allocated_by_the_later_invoice(order, po, admin_user):
    bill(po, 3, admin_user)
    assert DropShipAllocation.objects.count() == 0
    invoice(order, 3, admin_user, 9)
    assert billed_flags(order) == [True]
    assert drop_ship_awaiting_bill() == []


def test_invoice_void_releases_allocation(order, po, admin_user):
    first = invoice(order, 1, admin_user, 3)
    second = invoice(order, 2, admin_user, 4)
    bill(po, 3, admin_user)
    assert billed_flags(order) == [True, True]
    void_invoice(first, user=admin_user, reason="Wrong customer")
    assert DropShipAllocation.objects.filter(invoice_line__invoice=first).count() == 0
    assert [(a.invoice_line.invoice.number, a.quantity) for a in DropShipAllocation.objects.all()] == [(second.number, 2)]
    assert second.lines.get().drop_ship_billed is True


def test_invoice_edit_reruns_allocation(order, po, admin_user, gadget):
    inv = invoice(order, 3, admin_user, 3)
    bill(po, 2, admin_user)
    assert billed_flags(order) == [False]
    edit_invoice(inv, user=admin_user, date=d(10, 3), terms=None, ship_to=inv.ship_to, notes="", lines=[
        InvoiceLineSpec(gadget, 2, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg",
                        so_line=order.lines.get())])
    assert billed_flags(order) == [True]
    assert DropShipAllocation.objects.get().quantity == 2


def test_two_bills_fill_one_invoice(order, po, admin_user):
    invoice(order, 3, admin_user, 3)
    bill(po, 1, admin_user, "1")
    assert billed_flags(order) == [False]
    bill(po, 2, admin_user, "2")
    assert billed_flags(order) == [True]

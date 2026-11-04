"""One drop-ship PO line shared by two customers' orders: bills fill the oldest sales order first."""
from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer, Vendor
from handling.profit import order_profit
from purchasing import dropship
from purchasing.models import DropShipAllocation
from purchasing.posting.bills import BillItemSpec, bill_from_po, edit_bill, void_bill
from purchasing.posting.orders import POLineSpec, create_po
from sales.posting.invoices import invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal
DS = dict(fulfillment="DROP_SHIP", drop_ship_vendor="Acme Mfg")


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def shared(handling_setup, inv_settings, purchasing_settings, sales_settings, make_item, customer, admin_user):
    """Harbor's order is entered first but dated later, so Acme's (older) order is filled first."""
    vendor = Vendor.objects.create(name="Acme Mfg")
    gadget = make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")
    so_b = create_order(customer=Customer.objects.create(name="Harbor Plumbing"), date=d(10, 2), user=admin_user,
                        lines=[OrderLineSpec(gadget, 4, D("10.00"), **DS)])
    so_a = create_order(customer=customer, date=d(10, 1), user=admin_user,
                        lines=[OrderLineSpec(gadget, 6, D("10.00"), **DS)])
    po = create_po(vendor=vendor, date=d(10, 2), user=admin_user, lines=[
        POLineSpec(gadget, 10, D("4.00"), allocations=((so_b.lines.get(), 4), (so_a.lines.get(), 6)))])
    return so_a, so_b, po


def invoice(order, qty, user, day):
    order.refresh_from_db()
    return invoice_from_order(order, date=d(10, day), quantities={order.lines.get().pk: qty}, user=user)


def bill(po, qty, price, user, day, suffix):
    po.refresh_from_db()
    line = po.lines.get()
    return bill_from_po(po, date=d(10, day), vendor_invoice_number=suffix, quantities={line.pk: qty},
                        prices={line.pk: D(price)}, user=user)


def matched(order):
    """(bill vendor invoice number, invoice date, quantity) for the order's invoice lines."""
    return sorted(
        (a.bill_item_line.bill.vendor_invoice_number, a.invoice_line.invoice.date.day, a.quantity)
        for a in DropShipAllocation.objects.filter(invoice_line__so_line__order=order).select_related(
            "bill_item_line__bill", "invoice_line__invoice"))


def flags(order):
    return [l.drop_ship_billed for inv in order.invoices.order_by("date", "id") for l in inv.lines.all()]


def cost(order, day=31):
    p = order_profit(order.number, d(10, day))
    return p.cost, p.flags


def test_two_customers_on_one_po(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_b, 4, admin_user, 4)
    invoice(so_a, 6, admin_user, 5)
    bill(po, 10, "4.00", admin_user, 6, "V1")
    assert matched(so_a) == [("V1", 5, 6)]
    assert matched(so_b) == [("V1", 4, 4)]
    assert flags(so_a) == [True] and flags(so_b) == [True]
    assert cost(so_a) == (D("24.00"), [])
    assert cost(so_b) == (D("16.00"), [])


def test_two_bills_mixed_fill_the_oldest_order_first(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_b, 4, admin_user, 4)
    invoice(so_a, 6, admin_user, 5)
    bill(po, 4, "4.10", admin_user, 6, "V1")
    # The first bill is all Acme's: its order is older, though Harbor was invoiced first.
    assert matched(so_a) == [("V1", 5, 4)] and flags(so_a) == [False]
    assert matched(so_b) == [] and flags(so_b) == [False]
    assert cost(so_a) == (D("16.40"), ["Drop-ship bill not entered"])
    assert cost(so_b) == (D("0.00"), ["Drop-ship bill not entered"])

    bill(po, 6, "3.90", admin_user, 9, "V2")
    assert matched(so_a) == [("V1", 5, 4), ("V2", 5, 2)]
    assert matched(so_b) == [("V2", 4, 4)]
    assert flags(so_a) == [True] and flags(so_b) == [True]
    assert cost(so_a) == (D("24.20"), [])
    assert cost(so_b) == (D("15.60"), [])


def test_partial_bill_and_partial_invoices(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_a, 2, admin_user, 3)
    invoice(so_b, 1, admin_user, 4)
    bill(po, 7, "4.00", admin_user, 5, "V1")
    # Acme's 6 come first, Harbor's share is the last 1.
    assert matched(so_a) == [("V1", 3, 2)] and matched(so_b) == [("V1", 4, 1)]
    invoice(so_a, 4, admin_user, 6)
    invoice(so_b, 3, admin_user, 7)
    assert matched(so_a) == [("V1", 3, 2), ("V1", 6, 4)]
    assert matched(so_b) == [("V1", 4, 1)]
    assert flags(so_a) == [True, True] and flags(so_b) == [True, False]
    bill(po, 3, "4.00", admin_user, 8, "V2")
    assert matched(so_b) == [("V1", 4, 1), ("V2", 7, 3)] and flags(so_b) == [True, True]


def test_voiding_a_bill_moves_the_shares(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_a, 6, admin_user, 5)
    invoice(so_b, 4, admin_user, 5)
    first = bill(po, 4, "4.10", admin_user, 6, "V1")
    bill(po, 6, "3.90", admin_user, 9, "V2")
    void_bill(first, user=admin_user, reason="Wrong vendor invoice", date=d(10, 10))
    assert matched(so_a) == [("V2", 5, 6)] and flags(so_a) == [True]
    assert matched(so_b) == [] and flags(so_b) == [False]
    bill(po, 4, "4.00", admin_user, 12, "V3")
    assert matched(so_a) == [("V2", 5, 6)]
    assert matched(so_b) == [("V3", 5, 4)] and flags(so_b) == [True]
    assert cost(so_a) == (D("23.40"), []) and cost(so_b) == (D("16.00"), [])


def test_voiding_an_invoice_frees_its_share_for_the_same_order(shared, admin_user):
    so_a, so_b, po = shared
    first = invoice(so_a, 3, admin_user, 4)
    invoice(so_a, 3, admin_user, 5)
    invoice(so_b, 4, admin_user, 5)
    bill(po, 4, "4.10", admin_user, 6, "V1")
    bill(po, 6, "3.90", admin_user, 9, "V2")
    assert matched(so_a) == [("V1", 4, 3), ("V1", 5, 1), ("V2", 5, 2)]
    before_b = matched(so_b)
    void_invoice(first, user=admin_user, reason="Duplicate", date=d(10, 10))
    assert matched(so_a) == [("V1", 5, 3)]
    assert matched(so_b) == before_b == [("V2", 5, 4)]
    invoice(so_a, 3, admin_user, 11)
    assert matched(so_a) == [("V1", 5, 3), ("V1", 11, 1), ("V2", 11, 2)]
    assert flags(so_a) == [False, True, True]


def test_editing_a_bill_rebuilds_both_orders(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_a, 6, admin_user, 5)
    invoice(so_b, 4, admin_user, 5)
    b = bill(po, 10, "4.00", admin_user, 6, "V1")
    line = po.lines.get()
    edit_bill(b, user=admin_user, date=b.date, vendor_invoice_number="V1", reason="Price",
              item_lines=[BillItemSpec(line.item, 10, D("4.20"), po_line=line)], expense_lines=[])
    assert cost(so_a) == (D("25.20"), []) and cost(so_b) == (D("16.80"), [])
    assert sum(a.quantity for a in DropShipAllocation.objects.all()) == 10


def test_as_of_replay_matches_the_bills_dated_by_then(shared, admin_user):
    so_a, so_b, po = shared
    invoice(so_b, 4, admin_user, 4)
    invoice(so_a, 6, admin_user, 5)
    bill(po, 4, "4.10", admin_user, 6, "V1")
    bill(po, 6, "3.90", admin_user, 9, "V2")
    assert cost(so_a, 5) == (D("0.00"), ["Drop-ship bill not entered"])
    assert cost(so_a, 7) == (D("16.40"), ["Drop-ship bill not entered"])
    assert cost(so_b, 7) == (D("0.00"), ["Drop-ship bill not entered"])
    assert cost(so_a, 9) == (D("24.20"), []) and cost(so_b, 9) == (D("15.60"), [])


def test_bill_pools_follow_sales_order_date_then_id(shared, admin_user):
    so_a, so_b, po = shared
    bill(po, 8, "4.00", admin_user, 6, "V1")
    pools = dropship.bill_pools([so_a.lines.get().pk, so_b.lines.get().pk])
    assert [q for _, q in pools[so_a.lines.get().pk]] == [6]
    assert [q for _, q in pools[so_b.lines.get().pk]] == [2]

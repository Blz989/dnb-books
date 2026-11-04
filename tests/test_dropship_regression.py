"""Single-link drop-ship numbers, recorded on the code before shared purchase orders existed. Shared POs must
leave every one of these numbers as it was."""
from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer, Vendor
from handling.profit import orders
from purchasing.models import DropShipAllocation, PurchaseOrder
from purchasing.posting.bills import bill_from_po, void_bill
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import InvoiceLine
from sales.posting.invoices import invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order
from sales.reports import drop_ship_awaiting_bill

D = Decimal
DS = dict(fulfillment="DROP_SHIP", drop_ship_vendor="Acme Mfg")


def d(month, day):
    return date(2026, month, day)


def snapshot():
    return {
        "allocations": sorted(
            (a.bill_item_line.bill.vendor_invoice_number, a.invoice_line.invoice.date.isoformat(),
             a.invoice_line.item.sku, a.quantity)
            for a in DropShipAllocation.objects.select_related(
                "bill_item_line__bill", "invoice_line__invoice", "invoice_line__item")),
        "billed": sorted(
            (l.invoice.date.isoformat(), l.item.sku, l.drop_ship_billed)
            for l in InvoiceLine.objects.filter(fulfillment="DROP_SHIP").select_related("invoice", "item")),
        "awaiting": sorted((l.invoice.date.isoformat(), l.item.sku, l.quantity) for l in drop_ship_awaiting_bill()),
        "profit": {
            day.isoformat(): [
                (o.customer.name, str(o.sales), str(o.cost), str(o.profit), str(o.owed), o.flags) for o in orders(day)]
            for day in (d(10, 8), d(10, 13), d(10, 31))
        },
    }


EXPECTED = {
    "allocations": [("A1", "2026-10-04", "GADGET", 2), ("A2", "2026-10-14", "GADGET", 3),
                    ("B2", "2026-10-06", "GADGET", 3)],
    "billed": [("2026-10-04", "GADGET", True), ("2026-10-06", "GADGET", True), ("2026-10-10", "GADGET", False),
               ("2026-10-14", "GADGET", True), ("2026-10-16", "GADGET", False)],
    "awaiting": [("2026-10-16", "GADGET", 2)],
    "profit": {
        "2026-10-08": [("Acme Builders", "59.00", "22.20", "36.80", "20.24", []),
                       ("Harbor Plumbing", "29.25", "0.00", "29.25", "16.09", ["Drop-ship bill not entered"])],
        "2026-10-13": [("Acme Builders", "59.00", "22.20", "36.80", "20.24", []),
                       ("Harbor Plumbing", "29.25", "11.85", "17.40", "9.57", [])],
        "2026-10-31": [("Acme Builders", "87.50", "34.20", "53.30", "29.32", []),
                       ("Harbor Plumbing", "29.25", "11.85", "17.40", "9.57", []),
                       ("Harbor Plumbing", "19.50", "0.00", "19.50", "10.73", ["Drop-ship bill not entered"])],
    },
}


@pytest.fixture
def scenario(handling_setup, inv_settings, purchasing_settings, sales_settings, make_item, customer, admin_user):
    user = admin_user
    vendor = Vendor.objects.create(name="Acme Mfg")
    other = Customer.objects.create(name="Harbor Plumbing")
    gadget = make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")
    part = make_item("PART", sale_price="20.0000", purchase_cost="7.0000")
    stock_po = create_po(vendor=vendor, date=d(9, 28), user=user, lines=[POLineSpec(part, 10, D("7.00"))])
    bill_from_po(stock_po, date=d(9, 28), vendor_invoice_number="STOCK", user=user,
                 quantities={stock_po.lines.get().pk: 10})

    so_a = create_order(customer=customer, date=d(10, 1), user=user, lines=[
        OrderLineSpec(gadget, 5, D("9.50"), **DS), OrderLineSpec(part, 2, D("20.00"))])
    so_b = create_order(customer=other, date=d(10, 2), user=user, lines=[OrderLineSpec(gadget, 3, D("9.75"), **DS)])
    (po_a,) = create_drop_ship_pos(so_a, user=user, date=d(10, 2))
    (po_b,) = create_drop_ship_pos(so_b, user=user, date=d(10, 3))
    ga, pa = so_a.lines.all()
    (gb,) = so_b.lines.all()
    line_a, line_b = po_a.lines.get(), po_b.lines.get()

    invoice_from_order(so_a, date=d(10, 4), quantities={ga.pk: 2, pa.pk: 2}, user=user)
    bill_from_po(po_a, date=d(10, 5), vendor_invoice_number="A1", quantities={line_a.pk: 2},
                 prices={line_a.pk: D("4.10")}, user=user)
    invoice_from_order(so_b, date=d(10, 6), quantities={gb.pk: 3}, user=user)
    b1 = bill_from_po(po_b, date=d(10, 7), vendor_invoice_number="B1", quantities={line_b.pk: 3},
                      prices={line_b.pk: D("3.90")}, user=user)
    bill_from_po(po_a, date=d(10, 9), vendor_invoice_number="A2", quantities={line_a.pk: 3},
                 prices={line_a.pk: D("4.00")}, user=user)
    inv_a2 = invoice_from_order(so_a, date=d(10, 10), quantities={ga.pk: 3, pa.pk: 0}, user=user)
    void_bill(b1, user=user, reason="Wrong price", date=d(10, 11))
    po_b.refresh_from_db()
    bill_from_po(po_b, date=d(10, 12), vendor_invoice_number="B2", quantities={line_b.pk: 3},
                 prices={line_b.pk: D("3.95")}, user=user)
    void_invoice(inv_a2, user=user, reason="Wrong date", date=d(10, 13))
    so_a.refresh_from_db()
    invoice_from_order(so_a, date=d(10, 14), quantities={ga.pk: 3, pa.pk: 0}, user=user)
    # Invoiced but never billed: it stays on the awaiting-bill report.
    so_c = create_order(customer=other, date=d(10, 15), user=user, lines=[OrderLineSpec(gadget, 2, D("9.75"), **DS)])
    create_drop_ship_pos(so_c, user=user, date=d(10, 15))
    invoice_from_order(so_c, date=d(10, 16), quantities={so_c.lines.get().pk: 2}, user=user)
    return so_a, so_b


def test_single_link_numbers_are_unchanged(scenario):
    assert PurchaseOrder.objects.count() == 4
    assert snapshot() == EXPECTED


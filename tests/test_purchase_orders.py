from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Terms, Vendor
from items.models import ItemType
from purchasing.errors import PurchasingError
from purchasing.models import Bill, BillItemLine, BillLineKind, PurchaseOrder
from purchasing.posting.orders import (
    POLineSpec, billed_quantity, close_po, create_drop_ship_pos, create_po, drop_ship_candidates, po_remaining,
    refresh_po_status, reopen_po, update_po,
)
from sales.models import DocStatus, Fulfillment, OrderStatus
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def widget(make_item):
    return make_item("WIDGET", purchase_cost="0.5000")


@pytest.fixture
def service(make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     purchase_cost="12.0000")


@pytest.fixture
def po(vendor, admin_user, widget):
    return create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 1000, D("0.50"))])


def bill_against(po, qty, status=DocStatus.OPEN, number="BILL-00001"):
    line = po.lines.first()
    bill = Bill.objects.create(
        number=number, vendor=po.vendor, vendor_invoice_number="X", date=d(10, 5), terms=Terms.NET_30,
        due_date=d(11, 4), purchase_order=po, status=status, created_by=po.created_by)
    BillItemLine.objects.create(bill=bill, item=line.item, quantity=qty, price=line.price, amount=qty * line.price,
                                po_line=line, kind=BillLineKind.STOCK)
    return bill


def test_create_po_numbers_and_lines(po, widget):
    assert po.number == "PO-00001"
    assert po.status == OrderStatus.OPEN
    line = po.lines.get()
    assert (line.item, line.quantity, line.price, line.position) == (widget, 1000, D("0.5000"), 0)


def test_create_po_needs_a_line(vendor, admin_user):
    with pytest.raises(PurchasingError, match="A purchase order needs at least one line."):
        create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[])


def test_create_po_rejects_bad_lines(vendor, admin_user, widget):
    with pytest.raises(PurchasingError, match="Line 1: enter a quantity above zero."):
        create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 0, D("1.00"))])
    with pytest.raises(PurchasingError, match="Line 1: price can't be negative."):
        create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 1, D("-1.00"))])
    assert PurchaseOrder.objects.count() == 0


def test_remaining_and_auto_close(po, admin_user):
    line = po.lines.get()
    assert po_remaining(line) == 1000
    bill_against(po, 400)
    refresh_po_status(po)
    assert (billed_quantity(line), po_remaining(line), po.status) == (400, 600, OrderStatus.OPEN)
    bill_against(po, 600, number="BILL-00002")
    refresh_po_status(po)
    assert po.status == OrderStatus.CLOSED


def test_void_bills_do_not_count(po):
    bill_against(po, 400, status=DocStatus.VOID)
    assert billed_quantity(po.lines.get()) == 0


def test_exclude_bill_ignores_that_bill(po):
    bill = bill_against(po, 400)
    assert po_remaining(po.lines.get(), exclude_bill=bill) == 1000


def test_closed_po_reopens_when_a_bill_is_voided(po):
    bill = bill_against(po, 1000)
    refresh_po_status(po)
    assert po.status == OrderStatus.CLOSED
    bill.status = DocStatus.VOID
    bill.save()
    refresh_po_status(po)
    assert po.status == OrderStatus.OPEN


def test_manual_close_sticks_until_reopened(po, admin_user):
    close_po(po, admin_user)
    refresh_po_status(po)
    assert po.status == OrderStatus.CLOSED
    reopen_po(po, admin_user)
    assert po.status == OrderStatus.OPEN


def test_update_po_rules(po, admin_user, widget, service):
    line = po.lines.get()
    bill_against(po, 400)
    with pytest.raises(PurchasingError, match="Line 1: 400 already billed."):
        update_po(po, user=admin_user, lines=[POLineSpec(widget, 399, D("0.50"), line_id=line.pk)])
    with pytest.raises(PurchasingError, match="Line 1 has been billed and can't be removed."):
        update_po(po, user=admin_user, lines=[POLineSpec(service, 1, D("5.00"))])
    update_po(po, user=admin_user, lines=[
        POLineSpec(widget, 800, D("0.55"), line_id=line.pk), POLineSpec(service, 1, D("5.00"))])
    assert [(l.quantity, l.price) for l in po.lines.all()] == [(800, D("0.5500")), (1, D("5.0000"))]


def test_closed_po_cannot_be_changed(po, admin_user, widget):
    close_po(po, admin_user)
    with pytest.raises(PurchasingError, match="PO-00001 is closed. Reopen it to make changes."):
        update_po(po, user=admin_user, lines=[POLineSpec(widget, 5, D("1.00"))])


# ---- drop ship ----

@pytest.fixture
def two_vendors(db):
    acme = Vendor.objects.create(name="Acme Mfg")
    zed = Vendor.objects.create(name="Zed Parts")
    return acme, zed


@pytest.fixture
def sales_order(customer, admin_user, widget, service):
    return create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(widget, 10, D("9.50")),
        OrderLineSpec(service, 2, D("40.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        OrderLineSpec(widget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Zed Parts"),
        OrderLineSpec(service, 1, D("40.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="  acme   mfg "),
    ])


def test_candidates_group_by_vendor_text(sales_order):
    candidates = drop_ship_candidates(sales_order)
    assert sorted(candidates) == ["Acme Mfg", "Zed Parts", "acme   mfg"]
    assert [l.quantity for l in candidates["Zed Parts"]] == [3]


def test_two_vendors_make_two_pos(sales_order, two_vendors, admin_user):
    acme, zed = two_vendors
    pos = create_drop_ship_pos(sales_order, user=admin_user, date=d(10, 2))
    assert sorted(p.vendor.name for p in pos) == ["Acme Mfg", "Zed Parts"]
    by_vendor = {p.vendor: p for p in pos}
    assert by_vendor[acme].sales_orders() == [sales_order]
    # The two service lines are the same item at the same cost, so they share one PO line.
    (acme_line,) = by_vendor[acme].lines.all()
    assert acme_line.quantity == 3 and [a.quantity for a in acme_line.allocations.all()] == [2, 1]
    zed_line = by_vendor[zed].lines.get()
    assert zed_line.quantity == 3 and zed_line.price == D("0.5000")
    assert [(a.so_line.quantity, a.quantity) for a in zed_line.allocations.all()] == [(3, 3)]


def test_lines_already_on_a_po_are_skipped(sales_order, two_vendors, admin_user):
    create_drop_ship_pos(sales_order, user=admin_user, date=d(10, 2))
    assert drop_ship_candidates(sales_order) == {}
    assert create_drop_ship_pos(sales_order, user=admin_user, date=d(10, 3)) == []
    assert PurchaseOrder.objects.count() == 2


def test_unmatched_vendor_creates_nothing(sales_order, admin_user, db):
    Vendor.objects.create(name="Acme Mfg")
    with pytest.raises(PurchasingError, match="No vendor named Zed Parts. Choose a vendor for these lines.") as e:
        create_drop_ship_pos(sales_order, user=admin_user, date=d(10, 2))
    assert e.value.unresolved == ["Zed Parts"]
    assert PurchaseOrder.objects.count() == 0


def test_vendor_choices_resolve_unmatched_names(sales_order, admin_user, db):
    Vendor.objects.create(name="Acme Mfg")
    other = Vendor.objects.create(name="Zeta Parts Inc")
    pos = create_drop_ship_pos(sales_order, user=admin_user, date=d(10, 2), vendor_choices={"Zed Parts": other})
    assert sorted(p.vendor.name for p in pos) == ["Acme Mfg", "Zeta Parts Inc"]


# ---- drop ship from several sales orders ----

@pytest.fixture
def gadget(make_item):
    return make_item("GADGET", purchase_cost="4.0000")


@pytest.fixture
def two_orders(customer, admin_user, gadget, widget, two_vendors):
    from contacts.models import Customer

    harbor = Customer.objects.create(name="Harbor Plumbing")
    newer = create_order(customer=harbor, date=d(10, 2), user=admin_user, lines=[
        OrderLineSpec(gadget, 4, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="acme mfg"),
        OrderLineSpec(widget, 5, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Zed Parts"),
        OrderLineSpec(widget, 1, D("9.50")),
    ])
    older = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(widget, 2, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        OrderLineSpec(gadget, 6, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
    ])
    return older, newer


def test_candidates_across_orders(two_orders):
    older, newer = two_orders
    candidates = drop_ship_candidates([newer, older])
    assert sorted(candidates) == ["Acme Mfg", "Zed Parts", "acme mfg"]
    assert [(l.order, l.quantity) for l in candidates["Acme Mfg"]] == [(older, 2), (older, 6)]


def test_one_po_per_vendor_across_orders(two_orders, two_vendors, admin_user, gadget, widget):
    older, newer = two_orders
    acme, zed = two_vendors
    pos = create_drop_ship_pos([newer, older], user=admin_user, date=d(10, 3))
    assert [p.vendor for p in pos] == [acme, zed]
    acme_po, zed_po = pos
    assert acme_po.sales_orders() == [older, newer]
    rows = [(l.item, l.quantity, l.price, [(a.so_line.order, a.quantity) for a in l.allocations.all()])
            for l in acme_po.lines.all()]
    # The gadgets from both orders combine into one line; lines follow the oldest order's line order.
    assert rows == [
        (widget, 2, D("0.5000"), [(older, 2)]),
        (gadget, 10, D("4.0000"), [(older, 6), (newer, 4)]),
    ]
    assert [(l.quantity, [a.so_line.order for a in l.allocations.all()]) for l in zed_po.lines.all()] == [
        (5, [newer])]
    assert drop_ship_candidates([older, newer]) == {}


def test_single_order_is_the_same_path(two_orders, two_vendors, admin_user):
    older, newer = two_orders
    (po,) = create_drop_ship_pos(older, user=admin_user, date=d(10, 3))
    assert po.sales_orders() == [older]
    pos = create_drop_ship_pos([older, newer], user=admin_user, date=d(10, 4))
    assert [p.sales_orders() for p in pos] == [[newer], [newer]]


def test_unresolved_vendor_across_orders_creates_nothing(two_orders, admin_user):
    older, newer = two_orders
    Vendor.objects.filter(name="Zed Parts").delete()
    with pytest.raises(PurchasingError, match="No vendor named Zed Parts.") as e:
        create_drop_ship_pos([older, newer], user=admin_user, date=d(10, 3))
    assert e.value.unresolved == ["Zed Parts"]
    assert PurchaseOrder.objects.count() == 0


def test_editing_a_drop_ship_po_keeps_its_allocations(two_orders, two_vendors, admin_user, gadget, widget):
    older, newer = two_orders
    acme_po, _ = create_drop_ship_pos([older, newer], user=admin_user, date=d(10, 3))
    single, shared_line = acme_po.lines.all()
    update_po(acme_po, user=admin_user, notes="Rush", lines=[
        POLineSpec(widget, 3, D("0.50"), line_id=single.pk), POLineSpec(gadget, 10, D("4.10"), line_id=shared_line.pk)])
    # One sales order line: the allocation follows the new quantity.
    assert [(a.so_line.order, a.quantity) for a in single.allocations.all()] == [(older, 3)]
    assert [a.quantity for a in shared_line.allocations.all()] == [6, 4]
    with pytest.raises(PurchasingError, match="Line 2 is shared by several sales orders. Its quantity can't change."):
        update_po(acme_po, user=admin_user, lines=[
            POLineSpec(widget, 3, D("0.50"), line_id=single.pk),
            POLineSpec(gadget, 9, D("4.10"), line_id=shared_line.pk)])
    # Removing an unbilled shared line frees both orders' lines for another PO.
    update_po(acme_po, user=admin_user, lines=[POLineSpec(widget, 3, D("0.50"), line_id=single.pk)])
    assert sorted((l.order.number, l.quantity) for lines in drop_ship_candidates([older, newer]).values()
                  for l in lines) == sorted([(older.number, 6), (newer.number, 4)])


# ---- totals on screen ----

@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


def test_detail_shows_line_amounts_and_total_for_a_drop_ship_po(adm, customer, admin_user, make_item, db):
    Vendor.objects.create(name="Acme Mfg")
    a, b, c = make_item("DS-A", purchase_cost="108.00"), make_item("DS-B", purchase_cost="32.00"), make_item("DS-C", purchase_cost="120.00")
    so = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[
        OrderLineSpec(a, 2, D("150.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        OrderLineSpec(b, 1, D("50.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        OrderLineSpec(c, 1, D("160.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
    ])
    (po,) = create_drop_ship_pos(so, user=admin_user, date=d(10, 2))
    r = adm.get(f"/purchasing/orders/{po.number}/")
    assert r.context["total"] == D("368.00")
    assert [l["amount"] for l in r.context["lines"]] == [D("216.00"), D("32.00"), D("120.00")]
    html = r.content.decode()
    assert "<th class=\"num\">Amount</th>" in html and "216.00" in html and "368.00" in html
    assert f"368.00" in adm.get("/purchasing/orders/").content.decode()


def test_amounts_round_half_up_per_line_and_match_the_pdf(adm, vendor, admin_user, widget, make_item, sales_settings, purchasing_settings):
    from purchasing.pdf import render_po_html

    other = make_item("W2")
    # 3 x 1.2345 = 3.7035 -> 3.70; 1 x 0.0050 -> 0.01; 2 x 0.0050 -> 0.01. Total is the sum of rounded lines.
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[
        POLineSpec(widget, 3, D("1.2345")), POLineSpec(other, 1, D("0.0050")), POLineSpec(make_item("W3"), 2, D("0.0050"))])
    r = adm.get(f"/purchasing/orders/{po.number}/")
    assert [l["amount"] for l in r.context["lines"]] == [D("3.70"), D("0.01"), D("0.01")]
    assert r.context["total"] == D("3.72")
    assert "3.72" in render_po_html(po)
    assert "3.72" in adm.get("/purchasing/orders/").content.decode()


def test_list_total_is_per_order(adm, po, vendor, admin_user, widget):
    other = create_po(vendor=vendor, date=d(10, 5), user=admin_user, lines=[POLineSpec(widget, 4, D("2.50"))])
    rows = {r["order"].number: r["total"] for r in adm.get("/purchasing/orders/").context["rows"]}
    assert rows[other.number] == D("10.00") and rows[po.number] == sum(l.quantity * l.price for l in po.lines.all())

from datetime import date
from decimal import Decimal

import pytest

from sales.errors import OrderError
from sales.models import DocStatus, Fulfillment, Invoice, InvoiceLine, OrderStatus
from sales.posting.orders import (
    OrderLineSpec, close_order, create_order, invoiced_quantity, refresh_status, remaining, reopen_order,
    update_order,
)

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def widget(make_item):
    return make_item("WIDGET", sale_price="9.5000")


@pytest.fixture
def service(make_item, revenue):
    from items.models import ItemType

    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def order(customer, admin_user, widget, service):
    return create_order(
        customer=customer, date=d(10, 1), user=admin_user,
        lines=[
            OrderLineSpec(widget, 10, D("9.50")),
            OrderLineSpec(service, 2, D("40.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        ],
    )


def invoice_line(order, line, qty, number="INV-000001", status=DocStatus.OPEN, admin_user=None):
    inv, _ = Invoice.objects.get_or_create(
        number=number,
        defaults=dict(customer=order.customer, date=d(10, 2), due_date=d(11, 1), sales_order=order, status=status,
                      created_by=order.created_by))
    return InvoiceLine.objects.create(
        invoice=inv, item=line.item, quantity=qty, price=line.price, amount=line.price * qty, so_line=line)


def test_create_order_numbers_and_defaults_ship_to(order, customer):
    assert order.number == "SO-00001"
    assert order.status == OrderStatus.OPEN
    assert order.ship_to == customer.ship_to_address
    first, second = order.lines.all()
    assert (first.position, second.position) == (0, 1)
    assert second.fulfillment == Fulfillment.DROP_SHIP and second.drop_ship_vendor == "Acme Mfg"


def test_ship_to_can_be_overridden(customer, admin_user, widget):
    o = create_order(customer=customer, date=d(10, 1), user=admin_user, ship_to="Elsewhere", notes="Rush",
                     lines=[OrderLineSpec(widget, 1, D("1.00"))])
    assert o.ship_to == "Elsewhere" and o.notes == "Rush"


def test_quantity_must_be_above_zero(customer, admin_user, widget):
    with pytest.raises(OrderError, match="Line 2: enter a quantity above zero."):
        create_order(customer=customer, date=d(10, 1), user=admin_user,
                     lines=[OrderLineSpec(widget, 1, D("1.00")), OrderLineSpec(widget, 0, D("1.00"))])


def test_drop_ship_needs_vendor(customer, admin_user, widget):
    with pytest.raises(OrderError, match="Line 1: enter the drop-ship vendor."):
        create_order(customer=customer, date=d(10, 1), user=admin_user,
                     lines=[OrderLineSpec(widget, 1, D("1.00"), fulfillment=Fulfillment.DROP_SHIP)])


def test_failed_create_uses_no_number(customer, admin_user, widget):
    with pytest.raises(OrderError):
        create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[OrderLineSpec(widget, 0, D("1.00"))])
    o = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[OrderLineSpec(widget, 1, D("1.00"))])
    assert o.number == "SO-00001"


def test_order_needs_a_line(customer, admin_user):
    with pytest.raises(OrderError, match="An order needs at least one line."):
        create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[])


def test_update_changes_and_adds_and_deletes_lines(order, admin_user, widget):
    keep, drop = order.lines.all()
    update_order(order, user=admin_user, notes="Changed", lines=[
        OrderLineSpec(widget, 12, D("9.75"), line_id=keep.pk),
        OrderLineSpec(widget, 3, D("1.00")),
    ])
    lines = list(order.lines.all())
    assert [(l.quantity, l.price) for l in lines] == [(12, D("9.75")), (3, D("1.00"))]
    assert lines[0].pk == keep.pk
    assert not order.lines.filter(pk=drop.pk).exists()
    order.refresh_from_db()
    assert order.notes == "Changed"


def test_cannot_lower_below_invoiced(order, admin_user, widget):
    first, _ = order.lines.all()
    invoice_line(order, first, 6)
    with pytest.raises(OrderError, match="Line 1: 6 already invoiced."):
        update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 5, D("9.50"), line_id=first.pk)])
    update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 6, D("9.50"), line_id=first.pk)])


def test_cannot_delete_invoiced_line(order, admin_user, widget):
    first, second = order.lines.all()
    invoice_line(order, second, 1)
    with pytest.raises(OrderError, match="Line 2 has been invoiced and can't be removed."):
        update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 10, D("9.50"), line_id=first.pk)])


def test_closed_order_cannot_be_edited(order, admin_user, widget):
    close_order(order, admin_user)
    with pytest.raises(OrderError, match="SO-00001 is closed. Reopen it to make changes."):
        update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 1, D("1.00"))])
    reopen_order(order, admin_user)
    update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 1, D("1.00"))])


def test_line_from_another_order_is_refused(order, customer, admin_user, widget):
    other = create_order(customer=customer, date=d(10, 1), user=admin_user, lines=[OrderLineSpec(widget, 1, D("1.00"))])
    foreign = other.lines.first()
    with pytest.raises(OrderError):
        update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 1, D("1.00"), line_id=foreign.pk)])


def test_remaining_ignores_void_invoices_and_can_exclude_one(order):
    first, _ = order.lines.all()
    invoice_line(order, first, 6, "INV-000001")
    invoice_line(order, first, 3, "INV-000002", status=DocStatus.VOID)
    assert invoiced_quantity(first) == 6
    assert remaining(first) == 4
    assert remaining(first, exclude_invoice=Invoice.objects.get(number="INV-000001")) == 10


def test_auto_close_when_fully_invoiced_and_reopen_when_not(order):
    first, second = order.lines.all()
    invoice_line(order, first, 10)
    refresh_status(order)
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN
    invoice_line(order, second, 2)
    refresh_status(order)
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED
    Invoice.objects.filter(number="INV-000001").update(status=DocStatus.VOID)
    refresh_status(order)
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN


def test_manual_close_survives_refresh(order, admin_user):
    close_order(order, admin_user)
    refresh_status(order)
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED and order.closed_by == admin_user and order.closed_at




def test_cannot_delete_line_that_only_a_void_invoice_used(order, admin_user, widget):
    first, second = order.lines.all()
    invoice_line(order, second, 1, status=DocStatus.VOID)
    with pytest.raises(OrderError, match="Line 2 has been invoiced and can't be removed."):
        update_order(order, user=admin_user, lines=[OrderLineSpec(widget, 10, D("9.50"), line_id=first.pk)])

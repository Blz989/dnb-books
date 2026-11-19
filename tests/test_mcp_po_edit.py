"""MCP purchase order create and edit: price set on the order (separate from item cost), drop-ship orders."""
from datetime import date
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from contacts.models import Vendor
from mcpserver import write
from mcpserver.runtime import run
from purchasing.models import PurchaseOrder, PurchaseOrderAllocation
from purchasing.posting.orders import create_drop_ship_pos
from sales.models import Fulfillment
from sales.posting.orders import OrderLineSpec, create_order
from tests.test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (shared fixtures)

D = Decimal
DAY = date(2026, 10, 15)


def preview(user, op, payload):
    return run(ctx_for(user), op, write.preview, operation=op, payload=payload)


def confirm(user, p, document=""):
    return run(ctx_for(user), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"],
               confirm_document=document)


def make_po(owner, lines):
    p = preview(owner, "create_purchase_order", {"vendor": "Acme Supply", "date": "2026-10-05", "lines": lines})
    return p, confirm(owner, p)["document"]


@pytest.fixture
def dropship(books, owner):
    """A drop-ship PO: two sales order lines for WID-1 combined on one PO line, plus a separate service line."""
    lines = [OrderLineSpec(books.widget, 2, D("25.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Supply")]
    first = create_order(customer=books.customer, date=DAY, user=owner, lines=lines)
    second = create_order(customer=books.customer, date=DAY, user=owner, lines=[
        OrderLineSpec(books.widget, 3, D("25.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Supply")])
    (po,) = create_drop_ship_pos([first, second], user=owner, date=DAY)
    return po


def test_create_preview_shows_price_override_against_item_cost(books, owner):
    p, number = make_po(owner, [{"sku": "WID-1", "quantity": 5, "price": "12.50"},
                                {"sku": "WID-1", "quantity": 1}])
    over, plain = p["preview"]["lines"]
    assert (over["price"], over["item_cost"]) == ("12.50", "10.00")
    assert "differs from the item cost" in over["price_note"]
    assert plain["price"] == plain["item_cost"] == "10.00" and "price_note" not in plain
    po = PurchaseOrder.objects.get(number=number)
    assert [l.price for l in po.lines.all()] == [D("12.50"), D("10.00")]
    books.widget.refresh_from_db()
    assert books.widget.purchase_cost == D("10.00")  # the item cost is never changed


def test_edit_normal_po_prices_quantities_add_and_remove(books, owner):
    _, number = make_po(owner, [{"sku": "WID-1", "quantity": 5, "price": "12.50"}, {"sku": "SVC", "quantity": 1}])
    # No price given keeps the 12.50 the line had; the service line is removed; a new widget line is added at 9.00.
    p = preview(owner, "edit_purchase_order", {"number": number, "lines": [
        {"sku": "WID-1", "quantity": 8}, {"sku": "WID-1", "quantity": 2, "price": "9.00"}]})
    after = p["preview"]["after"]["lines"]
    assert [(l["quantity"], l["price"], l["item_cost"]) for l in after] == [(8, "12.50", "10.00"), (2, "9.00", "10.00")]
    confirm(owner, p, number)
    po = PurchaseOrder.objects.get(number=number)
    assert [(l.quantity, l.price) for l in po.lines.all()] == [(8, D("12.50")), (2, D("9.00"))]
    # A new price on an existing line replaces it.
    p = preview(owner, "edit_purchase_order", {"number": number, "lines": [
        {"sku": "WID-1", "quantity": 8, "price": "11.00"}, {"sku": "WID-1", "quantity": 2}]})
    confirm(owner, p, number)
    assert [l.price for l in po.lines.all()] == [D("11.00"), D("9.00")]
    books.widget.refresh_from_db()
    assert books.widget.purchase_cost == D("10.00")


def test_edit_drop_ship_po_keeps_allocations(books, owner, dropship):
    line = dropship.lines.get()
    assert sorted(a.quantity for a in line.allocations.all()) == [2, 3]
    # Price change keeps the line, its allocations, and its quantity.
    p = preview(owner, "edit_purchase_order", {"number": dropship.number, "lines": [
        {"sku": "WID-1", "quantity": 5, "price": "7.25"}]})
    assert "drop_ship" in p["preview"]
    assert p["preview"]["after"]["lines"][0]["allocations"]
    confirm(owner, p, dropship.number)
    line.refresh_from_db()
    assert line.price == D("7.25")
    assert sorted(a.quantity for a in line.allocations.all()) == [2, 3]


def test_edit_drop_ship_po_shared_line_quantity_cannot_change(books, owner, dropship):
    with pytest.raises(ToolError, match="shared by several sales orders"):
        preview(owner, "edit_purchase_order", {"number": dropship.number, "lines": [{"sku": "WID-1", "quantity": 4}]})


def test_edit_drop_ship_po_single_allocation_quantity_follows_line(books, owner):
    order = create_order(customer=books.customer, date=DAY, user=owner, lines=[
        OrderLineSpec(books.widget, 2, D("25.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Supply")])
    (po,) = create_drop_ship_pos(order, user=owner, date=DAY)
    p = preview(owner, "edit_purchase_order", {"number": po.number, "lines": [
        {"sku": "WID-1", "quantity": 4, "price": "6.00"}]})
    confirm(owner, p, po.number)
    alloc = PurchaseOrderAllocation.objects.get(po_line__order=po)
    assert alloc.quantity == 4 and alloc.po_line.price == D("6.00")


def test_edit_drop_ship_po_remove_line_frees_the_sales_order_line(books, owner, dropship):
    p = preview(owner, "edit_purchase_order", {"number": dropship.number, "lines": [{"sku": "SVC", "quantity": 1}]})
    confirm(owner, p, dropship.number)
    assert not PurchaseOrderAllocation.objects.exists()


def test_edit_po_still_needs_an_admin_and_the_typed_number(books, owner, clerk, dropship):
    with pytest.raises(ToolError):
        preview(clerk, "edit_purchase_order", {"number": dropship.number, "lines": [{"sku": "WID-1", "quantity": 5}]})
    p = preview(owner, "edit_purchase_order", {"number": dropship.number, "lines": [{"sku": "WID-1", "quantity": 5}]})
    with pytest.raises(ToolError, match="Type the document number"):
        confirm(owner, p, "")


def test_get_purchase_order_shows_item_cost(books, owner):
    from mcpserver import read
    _, number = make_po(owner, [{"sku": "WID-1", "quantity": 5, "price": "12.50"}])
    line = read.get_purchase_order(owner, number)["lines"][0]
    assert (line["price"], line["item_cost"]) == ("12.50", "10.00")

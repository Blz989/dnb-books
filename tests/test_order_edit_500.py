from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from contacts.models import Vendor
from purchasing.posting.orders import create_drop_ship_pos
from sales.models import Fulfillment
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


@pytest.fixture
def so(customer, admin_user, inv_settings, purchasing_settings, sales_settings, make_item):
    Vendor.objects.create(name="Acme Mfg")
    gadget = make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")
    widget = make_item("WIDGET", sale_price="5.0000")
    order = create_order(customer=customer, date=date(2026, 10, 1), user=admin_user, lines=[
        OrderLineSpec(gadget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        OrderLineSpec(widget, 2, D("5.00")),
    ], expected_delivery_date=date(2026, 11, 20))
    create_drop_ship_pos(order, user=admin_user, date=date(2026, 10, 2))
    return order


def post_data(order, keep_dropship=True):
    data = {"customer": order.customer_id, "date": "2026-10-01", "ship_to": "x", "notes": "",
            "expected_delivery_date": "2026-11-20", "token": "t1"}
    i = 0
    for line in order.lines.all():
        if line.fulfillment == Fulfillment.DROP_SHIP and not keep_dropship:
            continue
        data.update({f"lines-{i}-sku": line.item.sku, f"lines-{i}-quantity": str(line.quantity),
                     f"lines-{i}-price": "9.50", f"lines-{i}-fulfillment": line.fulfillment,
                     f"lines-{i}-vendor": line.drop_ship_vendor, f"lines-{i}-ref": str(line.pk)})
        i += 1
    return data


def test_get_edit_page(client, admin_user, so):
    client.force_login(admin_user)
    assert client.get(reverse("sales:order-edit", args=[so.number])).status_code == 200


def test_post_edit_keeps_lines(client, admin_user, so):
    client.force_login(admin_user)
    r = client.post(reverse("sales:order-edit", args=[so.number]), post_data(so))
    assert r.status_code == 302


def test_removing_allocated_dropship_line_is_a_clear_error(client, admin_user, so):
    client.force_login(admin_user)
    r = client.post(reverse("sales:order-edit", args=[so.number]), post_data(so, keep_dropship=False))
    assert r.status_code == 200
    assert b"is on purchase order PO-" in r.content
    assert so.lines.count() == 2


def test_switching_allocated_line_to_stock_is_blocked(client, admin_user, so):
    client.force_login(admin_user)
    data = post_data(so)
    data["lines-0-fulfillment"] = Fulfillment.STOCK
    r = client.post(reverse("sales:order-edit", args=[so.number]), data)
    assert r.status_code == 200 and b"has to stay a drop-ship line" in r.content

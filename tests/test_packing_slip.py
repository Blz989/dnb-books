import re
from datetime import date
from decimal import Decimal

import pytest

from items.models import ItemType
from purchasing.posting.bills import BillItemSpec, create_bill
from sales.models import Fulfillment
from sales.pdf import packing_slip_pdf, render_packing_slip_html
from sales.posting.invoices import InvoiceAdjustmentSpec, InvoiceLineSpec, create_invoice, void_invoice

D = Decimal


@pytest.fixture
def stock(inv_settings, sales_settings, purchasing_settings, make_item, vendor, admin_user):
    widget = make_item("WID-1", purchase_cost="4.00")
    bolt = make_item("BOLT-14", purchase_cost="0.50")
    create_bill(vendor=vendor, date=date(2026, 10, 1), vendor_invoice_number="S-1", user=admin_user,
                item_lines=[BillItemSpec(widget, 50, D("4.00")), BillItemSpec(bolt, 500, D("0.50"))])
    faucet = make_item("FAUCET", purchase_cost="38.00")
    sales_settings.company_name = "DNB Supply Co."
    sales_settings.save()
    return widget, bolt, faucet


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_stock_lines_without_prices(stock, customer, admin_user):
    widget, bolt, faucet = stock
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user, lines=[
        InvoiceLineSpec(widget, 3, D("19.99")), InvoiceLineSpec(bolt, 200, D("0.89")),
        InvoiceLineSpec(faucet, 2, D("69.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Delta Faucet")],
        adjustment=InvoiceAdjustmentSpec(D("-1.23")))
    t = text(render_packing_slip_html(inv))
    assert "PACKING SLIP" in t and inv.number in t and "DNB Supply Co." in t
    assert "WID-1" in t and "BOLT-14" in t and "200" in t
    assert "Total quantity 203" in t
    assert "Shipping separately from Delta Faucet: FAUCET × 2" in t
    assert "Received by" in t
    for money in ("19.99", "0.89", "69.00", "1.23", "Balance", "Total $", "Terms", "Due"):
        assert money not in t
    assert "Adjustment" not in t
    assert packing_slip_pdf(inv).startswith(b"%PDF")


def test_drop_ship_only(stock, customer, admin_user):
    _, _, faucet = stock
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user, lines=[
        InvoiceLineSpec(faucet, 2, D("69.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Delta Faucet")])
    t = text(render_packing_slip_html(inv))
    assert "Nothing in this box" in t and "Shipping separately from Delta Faucet: FAUCET × 2" in t


def test_void_mark(stock, customer, admin_user):
    widget, _, _ = stock
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(widget, 1, D("19.99"))])
    void_invoice(inv, user=admin_user, reason="Wrong")
    assert "void-mark" in render_packing_slip_html(inv)


def test_button_and_view(client, stock, customer, standard_user, admin_user):
    widget, _, _ = stock
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(widget, 1, D("19.99"))])
    client.force_login(standard_user)
    html = client.get(f"/sales/invoices/{inv.number}/").content.decode()
    assert f"/sales/invoices/{inv.number}/packing-slip/" in html and "Packing slip" in html
    resp = client.get(f"/sales/invoices/{inv.number}/packing-slip/")
    assert resp.status_code == 200 and resp["Content-Type"] == "application/pdf"
    assert f'{inv.number}-packing-slip.pdf' in resp["Content-Disposition"]

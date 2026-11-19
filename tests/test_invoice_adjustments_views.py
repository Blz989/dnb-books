"""Invoice screens and PDFs with the Adjustment field (adjustments spec, Task 6)."""
import re
from datetime import date
from decimal import Decimal

import pytest

from sales.balances import invoice_total
from sales.models import Invoice, InvoiceLayout, SalesOrder
from sales.pdf import render_invoice_html
from sales.posting.invoices import InvoiceAdjustmentSpec, InvoiceLineSpec, create_invoice, invoice_adjustment
from tests.test_order_invoice_views import adm, bolt, lines, order_post, service  # noqa: F401  (fixtures)

D = Decimal


def _text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def invoice_post(customer, *rows, token="tok-inv-1", **extra):
    return order_post(customer, *rows, token=token, terms="", **extra)


def adj(amount, note=""):
    return {"adj-amount": amount, "adj-note": note}


@pytest.fixture
def setup(inv_settings, sales_settings):
    return sales_settings


def test_new_invoice_with_adjustment(adm, setup, customer, service):
    assert "Adjustment note" in adm.get("/sales/invoices/new/").content.decode()
    r = adm.post("/sales/invoices/new/", invoice_post(customer, ("SVC", 2, "40.00"), **adj("-0.75", "Rounding")))
    assert r.status_code == 302
    inv = Invoice.objects.get()
    line = invoice_adjustment(inv)
    assert (line.amount, line.description, line.item.sku) == (D("-0.75"), "Rounding", "ADJ")
    assert invoice_total(inv) == D("79.25")
    page = _text(adm.get(f"/sales/invoices/{inv.number}/").content.decode())
    assert "Subtotal 80.00" in page and "Rounding Adjustment -0.75" in page and "Total 79.25" in page


@pytest.mark.parametrize("amount, message", [
    ("ten", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("0.125", "Adjustment: amounts can have at most 2 decimal places."),
    ("-80.01", "The invoice total can't be below zero."),
])
def test_form_errors(adm, setup, customer, service, amount, message):
    r = adm.post("/sales/invoices/new/", invoice_post(customer, ("SVC", 2, "40.00"), **adj(amount)))
    assert r.status_code == 200 and message in r.content.decode()
    assert not Invoice.objects.exists()


def test_edit_loads_changes_and_removes(adm, setup, customer, service, admin_user):
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(service, 2, D("40.00"))], adjustment=InvoiceAdjustmentSpec(D("5.00")))
    page = adm.get(f"/sales/invoices/{inv.number}/edit/").content.decode()
    assert 'name="adj-amount" value="5.00"' in page
    assert 'name="lines-1-sku" value=""' in page  # the adjustment is not a line row
    data = {**invoice_post(customer, ("SVC", 2, "40.00"), token="tok-e1", reason="Fix"), **adj("-2.00")}
    assert adm.post(f"/sales/invoices/{inv.number}/edit/", data).status_code == 302
    assert invoice_total(inv) == D("78.00") and inv.lines.filter(is_adjustment=True).count() == 1
    rev = _text(adm.get(f"/sales/invoices/{inv.number}/revisions/1/").content.decode())
    assert "ADJ Adjustment Adjustment 5.00" in rev
    data = {**invoice_post(customer, ("SVC", 2, "40.00"), token="tok-e2"), **adj("")}
    adm.post(f"/sales/invoices/{inv.number}/edit/", data)
    assert invoice_adjustment(inv) is None and invoice_total(inv) == D("80.00")


def test_invoice_from_order_with_adjustment(adm, setup, customer, bolt):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 10, "0.85")))
    order = SalesOrder.objects.get()
    line = order.lines.get()
    assert "Adjustment" in adm.get(f"/sales/orders/{order.number}/invoice/").content.decode()
    data = {"token": "tok-ship", "date": "2026-10-06", "ship_to": "", "notes": "", "terms": "",
            f"qty-{line.pk}": "10", **adj("-0.50")}
    assert adm.post(f"/sales/orders/{order.number}/invoice/", data).status_code == 302
    assert invoice_total(Invoice.objects.get()) == D("8.00")


@pytest.fixture
def adjusted(setup, customer, service, admin_user):
    return create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                          lines=[InvoiceLineSpec(service, 3, D("40.00"))],
                          adjustment=InvoiceAdjustmentSpec(D("-1.50"), "Rounding"))


@pytest.mark.parametrize("layout, subtotal", [(InvoiceLayout.CLASSIC, "Sub Total"), (InvoiceLayout.MODERN, "Subtotal")])
def test_pdf_totals_show_the_adjustment(adjusted, setup, layout, subtotal):
    setup.invoice_layout = layout
    setup.save()
    text = _text(render_invoice_html(adjusted))
    assert f"{subtotal} 120.00 Rounding -1.50" in text
    assert "118.50" in text
    assert text.count("ADJ") == 0  # not in the line table
    if layout == InvoiceLayout.MODERN:
        assert "Total quantity 3" in text
    else:
        assert "Items in Total 3" in text


def test_pdf_without_adjustment_has_no_subtotal(setup, customer, service, admin_user):
    inv = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(service, 3, D("40.00"))])
    for layout in InvoiceLayout:
        setup.invoice_layout = layout
        setup.save()
        text = _text(render_invoice_html(inv))
        assert "Sub Total" not in text and "Subtotal" not in text

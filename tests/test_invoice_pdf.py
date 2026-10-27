from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from contacts.models import Terms
from items.models import ItemType
from sales.models import Fulfillment, PaymentMethod
from sales.pdf import invoice_document, invoice_pdf, render_invoice_html
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.receipts import receive_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     name="Consulting hour")


@pytest.fixture
def invoice(service, customer, admin_user, sales_settings):
    sales_settings.company_name = "DNB Supply Co."
    sales_settings.company_address = "1450 Commerce Drive\nSpringfield, IL 62704"
    sales_settings.save()
    return create_invoice(
        customer=customer, date=d(10, 27), user=admin_user, notes="Please pay promptly.",
        lines=[
            InvoiceLineSpec(service, 2, D("40.00"), description="Install"),
            InvoiceLineSpec(service, 1, D("1240.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg"),
        ])


def test_html_has_the_invoice_facts(invoice):
    html = render_invoice_html(invoice)
    for text in ("INV-000001", "Acme Builders", "SVC", "Balance due", "Please pay promptly.", "DNB Supply Co.",
                 "Net 30", "Oct 27, 2026", "Nov 26, 2026", "1,320.00"):
        assert text in html, text
    assert "VOID" not in html


def test_drop_ship_line_has_the_note(invoice):
    assert html_count(render_invoice_html(invoice), "Shipped directly from the manufacturer") == 1


def html_count(html, text):
    return html.count(text)


def test_payments_and_credits_reduce_the_balance(invoice, customer, admin_user):
    receive_payment(customer=customer, date=d(10, 28), method=PaymentMethod.CHECK, amount=D("500.00"), user=admin_user,
                    applications=[(invoice, D("500.00"))])
    html = render_invoice_html(invoice)
    assert "−500.00" in html and "$820.00" in html


def test_sales_order_row_only_when_there_is_an_order(invoice):
    assert "Sales order" not in render_invoice_html(invoice)


def test_pdf_bytes(invoice):
    pdf = invoice_pdf(invoice)
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_sixty_line_invoice_renders_with_repeating_header(service, customer, admin_user):
    inv = create_invoice(customer=customer, date=d(10, 27), user=admin_user,
                         lines=[InvoiceLineSpec(service, n, D("1.50"), description=f"Line {n}") for n in range(1, 61)])
    assert "<thead>" in render_invoice_html(inv)
    assert len(invoice_document(inv).pages) >= 2
    assert invoice_pdf(inv).startswith(b"%PDF")


def test_void_invoice_has_watermark(invoice, admin_user):
    void_invoice(invoice, user=admin_user, reason="Mistake")
    html = render_invoice_html(invoice)
    assert "VOID" in html and invoice_pdf(invoice).startswith(b"%PDF")


def test_no_logo_shows_company_name_and_logo_shows_image(invoice, sales_settings, settings, tmp_path):
    assert "<img" not in render_invoice_html(invoice)
    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "logo").mkdir()
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
           b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")
    (tmp_path / "logo" / "logo.png").write_bytes(png)
    sales_settings.logo = "logo/logo.png"
    sales_settings.save()
    assert "<img" in render_invoice_html(invoice)
    assert invoice_pdf(invoice).startswith(b"%PDF")


def test_view_returns_inline_pdf(client, admin_user, invoice):
    url = reverse("sales:invoice-pdf", args=[invoice.number])
    assert client.get(url).status_code == 302
    client.force_login(admin_user)
    r = client.get(url)
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert r["Content-Disposition"] == 'inline; filename="INV-000001.pdf"'
    assert r.content.startswith(b"%PDF")


def test_view_unknown_number_is_404(client, admin_user):
    client.force_login(admin_user)
    assert client.get("/sales/invoices/INV-999999/pdf/").status_code == 404

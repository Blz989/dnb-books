import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from items.models import Category, ItemType
from sales.models import InvoiceLayout, PaymentMethod, SalesOrder, SalesSettings
from sales.pdf import invoice_document, invoice_pdf, render_invoice_html
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order, update_order
from sales.posting.receipts import receive_payment

D = Decimal
CONTINUED = "Continued on the next page"
NON_STOCK = dict(item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", name="Consulting hour", sale_price="40.00", **NON_STOCK)


@pytest.fixture
def modern(sales_settings):
    sales_settings.invoice_layout = InvoiceLayout.MODERN
    sales_settings.company_name = "DNB Supply Co."
    sales_settings.company_address = "1450 Commerce Drive\nSpringfield, IL 62704"
    sales_settings.save()
    return sales_settings


def _text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _page_texts(document):
    return [" ".join(b.text for b in page._page_box.descendants() if getattr(b, "text", None)) for page in document.pages]


# ---- Task 1: expected delivery date on sales orders ----

def test_order_saves_expected_delivery_date(service, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 5), user=admin_user, lines=[OrderLineSpec(service, 1, D("40.00"))],
                         expected_delivery_date=d(10, 9))
    assert SalesOrder.objects.get(pk=order.pk).expected_delivery_date == d(10, 9)
    plain = create_order(customer=customer, date=d(10, 5), user=admin_user, lines=[OrderLineSpec(service, 1, D("40.00"))])
    assert plain.expected_delivery_date is None


def test_update_order_changes_clears_and_keeps_the_date(service, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 5), user=admin_user, lines=[OrderLineSpec(service, 1, D("40.00"))],
                         expected_delivery_date=d(10, 9))
    specs = [OrderLineSpec(service, 1, D("40.00"), line_id=order.lines.first().pk)]
    update_order(order, lines=specs, user=admin_user)
    assert order.expected_delivery_date == d(10, 9)
    update_order(order, lines=specs, user=admin_user, expected_delivery_date=d(10, 12))
    assert order.expected_delivery_date == d(10, 12)
    update_order(order, lines=specs, user=admin_user, expected_delivery_date=None)
    assert order.expected_delivery_date is None


def _order_post(customer, **extra):
    data = {"token": "tok-layout-1", "customer": customer.pk, "date": "2026-10-05", "ship_to": "", "notes": "",
            "lines-0-sku": "SVC", "lines-0-quantity": "1", "lines-0-price": "40.00", "lines-0-description": "",
            "lines-0-fulfillment": "STOCK", "lines-0-vendor": ""}
    data.update(extra)
    return data


def test_order_form_saves_and_shows_expected_delivery(adm, service, customer):
    assert "Expected delivery" in adm.get("/sales/orders/new/").content.decode()
    r = adm.post("/sales/orders/new/", _order_post(customer, expected_delivery_date="2026-10-09"))
    assert r.status_code == 302
    order = SalesOrder.objects.get()
    assert order.expected_delivery_date == d(10, 9)
    page = adm.get(f"/sales/orders/{order.number}/").content.decode()
    assert "Expected delivery" in page and "2026-10-09" in page
    assert 'value="2026-10-09"' in adm.get(f"/sales/orders/{order.number}/edit/").content.decode()
    line = order.lines.first()
    r = adm.post(f"/sales/orders/{order.number}/edit/",
                 _order_post(customer, expected_delivery_date="", **{"lines-0-ref": str(line.pk)}))
    assert r.status_code == 302
    order.refresh_from_db()
    assert order.expected_delivery_date is None


def test_invoice_form_has_no_expected_delivery_field(adm, service, customer):
    assert "Expected delivery" not in adm.get("/sales/invoices/new/").content.decode()


# ---- Task 2: the layout setting ----

def test_layout_defaults_to_classic(sales_settings):
    assert SalesSettings.load().invoice_layout == InvoiceLayout.CLASSIC


def _settings_post(s, **extra):
    return {"action": "settings", "ar_account": s.ar_account_id, "undeposited_account": s.undeposited_account_id,
            "returns_account": s.returns_account_id, "default_invoice_notes": "", **extra}


def test_admin_changes_the_layout_in_settings(adm, sales_settings):
    page = adm.get("/sales/settings/").content.decode()
    assert "Invoice layout" in page and "Applies to every invoice each time it is printed." in page
    assert adm.post("/sales/settings/", _settings_post(sales_settings, invoice_layout="MODERN")).status_code == 302
    assert SalesSettings.load().invoice_layout == InvoiceLayout.MODERN
    # a post without the field keeps the current choice
    assert adm.post("/sales/settings/", _settings_post(sales_settings)).status_code == 302
    assert SalesSettings.load().invoice_layout == InvoiceLayout.MODERN
    assert adm.post("/sales/settings/", _settings_post(sales_settings, invoice_layout="CLASSIC")).status_code == 302
    assert SalesSettings.load().invoice_layout == InvoiceLayout.CLASSIC


# ---- Task 3: the Modern layout ----

@pytest.fixture
def invoice(service, customer, admin_user):
    return create_invoice(customer=customer, date=d(10, 27), user=admin_user, notes="COD",
                          lines=[InvoiceLineSpec(service, 2, D("40.00"), description="Install"),
                                 InvoiceLineSpec(service, 1, D("1240.00"))])


def test_classic_is_unchanged_when_the_setting_is_classic(invoice):
    html = render_invoice_html(invoice)
    assert "Authorized Signature" in html and "Customer sign-off" not in html


def test_modern_has_the_invoice_facts(invoice, modern):
    text = _text(render_invoice_html(invoice))
    for part in ("DNB Supply Co.", "1450 Commerce Drive", "Invoice", "INV-000001", "Invoice date", "Oct 27, 2026",
                 "Terms", "Net 30", "Due date", "Nov 26, 2026", "Balance due", "$1,320.00", "Bill to", "Ship to",
                 "Acme Builders", "Reference", "Invoice no.", "Packaging", "Qty", "Unit", "Price", "Amount", "Install",
                 "Consulting hour", "Total quantity 3", "Invoice total 1,320.00", "Notes COD", "Customer sign-off",
                 "Check No.", "Cash", "Zelle", "Card", "Not yet paid", "Amount paid $", "Customer signature",
                 "Printed name"):
        assert part in text, part
    for gone in ("Authorized Signature", "ACH", "Questions", "Sales order", "Expected delivery", "Payments and credits",
                 "How to pay", "Check (preferred)", "Payable to", "on your payment", "How paid", "check one"):
        assert gone not in text, gone
    assert "VOID" not in text
    assert invoice_pdf(invoice).startswith(b"%PDF")


def test_modern_reference_shows_the_sales_order_and_expected_delivery(service, customer, admin_user, modern):
    order = create_order(customer=customer, date=d(10, 20), user=admin_user, lines=[OrderLineSpec(service, 1, D("10.00"))],
                         expected_delivery_date=d(10, 29))
    inv = invoice_from_order(order, date=d(10, 27), user=admin_user, quantities={order.lines.first().pk: 1})
    text = _text(render_invoice_html(inv))
    assert f"Sales order {order.number}" in text and "Expected delivery Oct 29, 2026" in text
    order.expected_delivery_date = None
    order.save()
    text = _text(render_invoice_html(inv))
    assert f"Sales order {order.number}" in text and "Expected delivery" not in text


def test_modern_shows_payments_and_the_balance(invoice, customer, admin_user, modern):
    receive_payment(customer=customer, date=d(10, 28), method=PaymentMethod.CHECK, amount=D("500.00"), user=admin_user,
                    applications=[(invoice, D("500.00"))])
    text = _text(render_invoice_html(invoice))
    assert "Payments and credits −500.00" in text and "$820.00" in text and "Invoice total 1,320.00" in text


def test_modern_groups_lines_by_category_and_lowercases_the_unit(make_item, customer, admin_user, modern):
    dry, frozen = Category.objects.create(name="Dry Goods"), Category.objects.create(name="Refrigerated/Frozen Goods")
    a = make_item("RICE", category=frozen, packaging="30LBS", unit="CASE", name="Marinated baby", **NON_STOCK)
    b = make_item("BEAN", category=dry, packaging="25X16OZ", unit="CASE", name="Rice stick", **NON_STOCK)
    inv = create_invoice(customer=customer, date=d(10, 27), user=admin_user,
                         lines=[InvoiceLineSpec(a, 1, D("145.00")), InvoiceLineSpec(b, 2, D("80.00"))])
    html = render_invoice_html(inv)
    order = [html.index(x) for x in ("Dry Goods", "Rice stick", "25X16OZ", "Refrigerated/Frozen Goods", "Marinated baby")]
    assert order == sorted(order)
    assert ">case<" in html and ">CASE<" not in html


def test_modern_without_notes_has_no_notes_heading(service, customer, admin_user, modern):
    inv = create_invoice(customer=customer, date=d(10, 27), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("40.00"))])
    assert "Notes" not in _text(render_invoice_html(inv))


def test_modern_single_page_has_no_continued_line_and_no_running_header(invoice, modern):
    pages = _page_texts(invoice_document(invoice))
    assert len(pages) == 1
    assert CONTINUED not in pages[0] and "Page 1 of 1" in pages[0]
    assert pages[0].count("Customer sign-off") == 1


def test_modern_void_invoice_has_the_watermark(invoice, admin_user, modern):
    void_invoice(invoice, user=admin_user, reason="Mistake")
    assert "VOID" in render_invoice_html(invoice) and invoice_pdf(invoice).startswith(b"%PDF")


def _long_invoice(service, customer, admin_user, count):
    return create_invoice(customer=customer, date=d(10, 27), user=admin_user, lines=[
        InvoiceLineSpec(service, 1, D("10.00"), description=f"Line {n:03d}") for n in range(1, count + 1)])


@pytest.mark.parametrize("count", [30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 70, 110])
def test_modern_page_breaks(service, customer, admin_user, modern, count):
    """Every length from 'just fits' to 'closing block alone on the last page' keeps the rules of spec section 2.5."""
    pages = _page_texts(invoice_document(_long_invoice(service, customer, admin_user, count)))
    total = len(pages)
    seen = []
    for number, page in enumerate(pages, 1):
        assert f"Page {number} of {total}" in page
        lines = re.findall(r"Line \d{3}", page)
        seen += lines
        if lines:
            assert "Packaging" in page and "Amount" in page  # column headings repeat
        assert ("Bill to" in page) == (number == 1)  # the full header is on page 1 only
        if number > 1:
            assert "Acme Builders" in page and "INV-000001" in page  # running header
        assert (CONTINUED in page) == (number < total)
    assert seen == [f"Line {n:03d}" for n in range(1, count + 1)]  # no line lost, repeated, or split
    last = pages[-1]
    for part in ("Total quantity", "Invoice total", "Customer sign-off", "Customer signature", "Printed name"):
        assert part in last and all(part not in p for p in pages[:-1]), part


def test_modern_long_invoice_has_several_pages(service, customer, admin_user, modern):
    assert len(invoice_document(_long_invoice(service, customer, admin_user, 110)).pages) >= 3


def test_modern_category_heading_never_ends_a_page(make_item, customer, admin_user, modern):
    """Try every position of the heading around the first page break; it must always have a line after it."""
    first = Category.objects.create(name="Aaa First")
    second = Category.objects.create(name="Zzz Second")
    a = make_item("A1", category=first, name="Alpha", **NON_STOCK)
    b = make_item("B1", category=second, name="Beta", **NON_STOCK)
    for count in range(24, 36):
        inv = create_invoice(customer=customer, date=d(10, 27), user=admin_user,
                             lines=[InvoiceLineSpec(a, 1, D("1.00"), description=f"Line {n:03d}") for n in range(count)]
                             + [InvoiceLineSpec(b, 1, D("1.00"), description=f"Tail {n:03d}") for n in range(40)])
        for page in _page_texts(invoice_document(inv)):
            if "Zzz Second" in page:
                assert "Tail 000" in page, count

import html
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from items.models import ItemType, MovementKind
from items.stock import StockLine, on_hand, record_movements
from sales.models import DocStatus, Invoice, InvoiceRevision, OrderStatus, SalesOrder
from sales.posting.invoices import InvoiceLineSpec, create_invoice

D = Decimal


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def bolt(inv_settings, sales_settings, admin_user, make_item):
    item = make_item("BOLT", sale_price="0.8500")
    record_movements(kind=MovementKind.OPENING, date=date(2026, 10, 1), user=admin_user,
                     lines=[StockLine(item, 10, unit_cost=D("0.50"))])
    return item


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     sale_price="40.00")


def lines(*rows, start=0):
    """rows: (sku, qty, price, description, fulfillment, vendor)"""
    data = {}
    for i, (sku, qty, *rest) in enumerate(rows, start):
        price, description, fulfillment, vendor = (list(rest) + ["", "", "STOCK", ""][len(rest):])[:4]
        data.update({f"lines-{i}-sku": sku, f"lines-{i}-quantity": str(qty), f"lines-{i}-price": price,
                     f"lines-{i}-description": description, f"lines-{i}-fulfillment": fulfillment, f"lines-{i}-vendor": vendor})
    return data


def order_post(customer, *rows, token="tok-order-1", **extra):
    data = {"token": token, "customer": customer.pk, "date": "2026-10-05", "ship_to": "", "notes": "", **lines(*rows)}
    data.update(extra)
    return data


def test_pages_need_login(client, customer):
    for url in ("/sales/orders/", "/sales/orders/new/", "/sales/invoices/", "/sales/invoices/new/", "/sales/orders/SO-00001/",
                "/sales/invoices/INV-000001/"):
        assert client.get(url).status_code == 302


def test_create_order_and_prefill_price(adm, customer, bolt, service):
    r = adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 10, ""), ("SVC", 2, "", "Delivery", "DROP_SHIP", "Acme Mfg")))
    assert r.status_code == 302 and r["Location"] == "/sales/orders/SO-00001/"
    order = SalesOrder.objects.get()
    first, second = order.lines.all()
    assert first.price == D("0.8500") and second.price == D("40.0000") and second.drop_ship_vendor == "Acme Mfg"
    assert order.ship_to == customer.ship_to_address
    page = adm.get("/sales/orders/SO-00001/").content.decode()
    assert "SO-00001" in page and "Invoice a shipment" in page and "Acme Mfg" in page


def test_order_form_errors_keep_the_rows(adm, customer, bolt):
    r = adm.post("/sales/orders/new/", order_post(customer, ("NOPE", 3, "1.00")))
    page = r.content.decode()
    assert r.status_code == 200 and "Line 1: no item has SKU NOPE." in page and 'value="NOPE"' in page
    r = adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 0, "1.00")))
    assert "Line 1: enter a quantity above zero." in r.content.decode()
    r = adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 1, "1.00", "", "DROP_SHIP", "")))
    assert "Line 1: enter the drop-ship vendor." in r.content.decode()
    assert not SalesOrder.objects.exists()


def test_order_needs_customer_and_lines(adm, bolt):
    r = adm.post("/sales/orders/new/", {"token": "t", "date": "2026-10-05", "customer": ""})
    assert "Choose a customer." in r.content.decode()


def test_order_double_submit_saves_once(adm, customer, bolt):
    data = order_post(customer, ("BOLT", 5, ""), token="same-token")
    adm.post("/sales/orders/new/", data)
    r = adm.post("/sales/orders/new/", data, follow=True)
    assert SalesOrder.objects.count() == 1 and "This order was already saved." in r.content.decode()


def test_edit_order(adm, customer, bolt):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 10, "")))
    line = SalesOrder.objects.get().lines.get()
    page = adm.get("/sales/orders/SO-00001/edit/").content.decode()
    assert 'value="BOLT"' in page
    data = {"customer": customer.pk, "date": "2026-10-06", "ship_to": "", "notes": "Updated",
            **lines(("BOLT", 12, "0.85")), "lines-0-ref": str(line.pk)}
    assert adm.post("/sales/orders/SO-00001/edit/", data).status_code == 302
    line.refresh_from_db()
    assert line.quantity == 12 and SalesOrder.objects.get().notes == "Updated"


def test_invoice_a_shipment_through_the_views(adm, customer, bolt, service):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 10, ""), ("SVC", 1, "40.00")))
    l1, l2 = SalesOrder.objects.get().lines.all()
    page = adm.get("/sales/orders/SO-00001/invoice/").content.decode()
    assert f'name="qty-{l1.pk}" value="10"' in page and "10 on hand" in page
    r = adm.post("/sales/orders/SO-00001/invoice/", {
        "token": "ship-1", "date": "2026-10-07", "terms": "NET_15", "ship_to": "Job site", "notes": "Hi",
        f"qty-{l1.pk}": "6", f"qty-{l2.pk}": "0"})
    assert r.status_code == 302 and r["Location"] == "/sales/invoices/INV-000001/"
    inv = Invoice.objects.get()
    assert inv.terms == "NET_15" and inv.due_date == date(2026, 10, 22) and inv.ship_to == "Job site"
    assert inv.lines.count() == 1 and on_hand(bolt) == 4
    assert SalesOrder.objects.get().status == OrderStatus.OPEN


def test_over_invoicing_shows_the_message(adm, customer, bolt):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 10, "")))
    (l1,) = SalesOrder.objects.get().lines.all()
    adm.post("/sales/orders/SO-00001/invoice/", {"token": "a", "date": "2026-10-07", f"qty-{l1.pk}": "6"})
    r = adm.post("/sales/orders/SO-00001/invoice/", {"token": "b", "date": "2026-10-07", f"qty-{l1.pk}": "5"})
    assert "Line 1: only 4 left to invoice on SO-00001." in r.content.decode()
    assert Invoice.objects.count() == 1


def test_negative_stock_confirm_flow(adm, customer, bolt):
    data = {"token": "neg-1", "customer": customer.pk, "date": "2026-10-07", "terms": "", "ship_to": "", "notes": "",
            **lines(("BOLT", 12, "0.85"))}
    r = adm.post("/sales/invoices/new/", data)
    page = r.content.decode()
    assert r.status_code == 200 and "Line 1: BOLT has 10 on hand. Saving will take it to -2." in page
    assert "Save anyway" in page and 'name="lines-0-sku" value="BOLT"' in page
    assert not Invoice.objects.exists()
    r = adm.post("/sales/invoices/new/", {**data, "allow_negative": "1"})
    assert r.status_code == 302 and on_hand(bolt) == -2


def test_negative_confirm_go_back_keeps_the_form(adm, customer, bolt):
    data = {"token": "neg-2", "customer": customer.pk, "date": "2026-10-07", "terms": "", "ship_to": "", "notes": "",
            **lines(("BOLT", 12, "0.85"))}
    r = adm.post("/sales/invoices/new/", {**data, "back": "1"})
    assert r.status_code == 200 and 'value="BOLT"' in r.content.decode() and "Save invoice" in r.content.decode()


def test_double_submit_posts_once(adm, customer, service):
    data = {"token": "dbl", "customer": customer.pk, "date": "2026-10-07", "terms": "", "ship_to": "", "notes": "",
            **lines(("SVC", 1, "40.00"))}
    adm.post("/sales/invoices/new/", data)
    r = adm.post("/sales/invoices/new/", data, follow=True)
    assert Invoice.objects.count() == 1 and "This invoice was already saved." in r.content.decode()


def test_inactive_item_gives_a_message_not_a_500(adm, customer, bolt):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 5, "")))
    (l1,) = SalesOrder.objects.get().lines.all()
    bolt.is_active = False
    bolt.save()
    r = adm.post("/sales/orders/SO-00001/invoice/", {"token": "i", "date": "2026-10-07", f"qty-{l1.pk}": "5"})
    assert r.status_code == 200 and "Line 1: BOLT is not an active stock item." in r.content.decode()


def test_settings_missing_links_to_settings(adm, customer, make_item):
    item = make_item("X1", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)
    data = {"token": "m", "customer": customer.pk, "date": "2026-10-07", "terms": "", "ship_to": "", "notes": "", **lines(("X1", 1, "5.00"))}
    page = adm.post("/sales/invoices/new/", data).content.decode()
    assert "Set up sales accounts first." in page and "/sales/settings/" in page


@pytest.fixture
def invoice(bolt, customer, admin_user):
    return create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user,
                          lines=[InvoiceLineSpec(bolt, 6, D("0.85"))], notes="Original")


def edit_data(customer, token, *rows, **extra):
    data = {"token": token, "customer": customer.pk, "date": "2026-10-05", "terms": "NET_30", "ship_to": "", "notes": "Edited",
            "reason": "Fewer bolts", **lines(*rows)}
    data.update(extra)
    return data


def test_edit_creates_a_revision_and_the_revision_page_shows_old_lines(adm, customer, invoice, bolt):
    page = adm.get("/sales/invoices/INV-000001/edit/").content.decode()
    assert 'value="BOLT"' in page and 'name="lines-0-quantity" value="6"' in page
    r = adm.post("/sales/invoices/INV-000001/edit/", edit_data(customer, "e1", ("BOLT", 3, "0.85")))
    assert r.status_code == 302
    invoice.refresh_from_db()
    assert invoice.revision == 2 and invoice.notes == "Edited" and on_hand(bolt) == 7
    detail = adm.get("/sales/invoices/INV-000001/").content.decode()
    assert "Revision 1" in detail and "/sales/invoices/INV-000001/revisions/1/" in detail
    old = adm.get("/sales/invoices/INV-000001/revisions/1/").content.decode()
    assert "BOLT" in old and ">6<" in old and "Original" in old and "Fewer bolts" in old
    assert adm.get("/sales/invoices/INV-000001/revisions/9/").status_code == 404


def test_standard_user_can_edit(std, customer, invoice, bolt):
    assert std.post("/sales/invoices/INV-000001/edit/", edit_data(customer, "e2", ("BOLT", 2, "0.85"))).status_code == 302


def test_edit_double_submit_makes_one_revision(adm, customer, invoice, bolt):
    data = edit_data(customer, "e3", ("BOLT", 3, "0.85"))
    adm.post("/sales/invoices/INV-000001/edit/", data)
    adm.post("/sales/invoices/INV-000001/edit/", data)
    assert InvoiceRevision.objects.count() == 1


def test_edit_below_applied_shows_message(adm, customer, invoice, bolt, admin_user):
    from sales.models import PaymentMethod
    from sales.posting.receipts import receive_payment

    receive_payment(customer=customer, date=date(2026, 10, 6), method=PaymentMethod.CHECK, amount=D("5.10"), user=admin_user,
                    applications=[(invoice, D("5.10"))])
    r = adm.post("/sales/invoices/INV-000001/edit/", edit_data(customer, "e4", ("BOLT", 3, "0.85")))
    assert "This invoice has 5.10 applied. The new total can't be less than that." in html.unescape(r.content.decode())


def test_detail_shows_applications_with_remove(adm, customer, invoice, admin_user):
    from sales.models import PaymentMethod
    from sales.posting.receipts import receive_payment

    receive_payment(customer=customer, date=date(2026, 10, 6), method=PaymentMethod.CHECK, amount=D("2.00"), user=admin_user,
                    applications=[(invoice, D("2.00"))])
    page = adm.get("/sales/invoices/INV-000001/").content.decode()
    assert "PMT-00001" in page and "Remove" in page and "Download PDF" in page and "JE-" in page
    app = invoice.payment_applications.get()
    r = adm.post(f"/sales/invoices/INV-000001/applications/payment/{app.pk}/remove/", follow=True)
    assert "Application removed." in r.content.decode()
    app.refresh_from_db()
    assert app.removed


def test_void_is_admin_only(std, adm, invoice):
    assert std.get("/sales/invoices/INV-000001/void/").status_code == 403
    assert std.post("/sales/invoices/INV-000001/void/", {"date": "2026-10-05", "reason": "x"}).status_code == 403
    page = adm.get("/sales/invoices/INV-000001/void/").content.decode()
    assert "Void invoice" in page
    r = adm.post("/sales/invoices/INV-000001/void/", {"date": "2026-10-05", "reason": ""})
    assert "Enter a reason." in r.content.decode()
    r = adm.post("/sales/invoices/INV-000001/void/", {"date": "2026-10-05", "reason": "Mistake"})
    assert r.status_code == 302
    invoice.refresh_from_db()
    assert invoice.status == DocStatus.VOID
    detail = adm.get("/sales/invoices/INV-000001/").content.decode()
    assert "Voided on 2026-10-05: Mistake" in detail


def test_void_button_hidden_for_standard(std, invoice):
    assert "/void/" not in std.get("/sales/invoices/INV-000001/").content.decode()


def test_void_blocked_message_with_applications(adm, customer, invoice, admin_user):
    from sales.models import PaymentMethod
    from sales.posting.receipts import receive_payment

    receive_payment(customer=customer, date=date(2026, 10, 6), method=PaymentMethod.CHECK, amount=D("2.00"), user=admin_user,
                    applications=[(invoice, D("2.00"))])
    r = adm.post("/sales/invoices/INV-000001/void/", {"date": "2026-10-06", "reason": "Mistake"})
    assert "INV-000001 has payments or credits applied. Remove them first." in r.content.decode()


def test_unknown_numbers_are_404(adm):
    for url in ("/sales/orders/SO-99999/", "/sales/orders/SO-99999/edit/", "/sales/invoices/INV-999999/",
                "/sales/invoices/INV-999999/edit/", "/sales/invoices/INV-999999/void/", "/sales/orders/SO-99999/invoice/"):
        r = adm.get(url)
        assert r.status_code == 404, url


def test_close_and_reopen_order(adm, customer, bolt):
    adm.post("/sales/orders/new/", order_post(customer, ("BOLT", 5, "")))
    adm.post("/sales/orders/SO-00001/close/")
    assert SalesOrder.objects.get().status == OrderStatus.CLOSED
    page = adm.get("/sales/orders/SO-00001/").content.decode()
    assert "Reopen order" in page and "Invoice a shipment" not in page
    adm.post("/sales/orders/SO-00001/close/")
    assert SalesOrder.objects.get().status == OrderStatus.OPEN


def test_lists_and_filters(adm, customer, invoice, bolt, admin_user):
    from django.utils import timezone

    old = create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(bolt, 1, D("5.00"))])
    recent = create_invoice(customer=customer, date=timezone.localdate(), user=admin_user, lines=[InvoiceLineSpec(bolt, 1, D("5.00"))])
    page = adm.get("/sales/invoices/").content.decode()
    assert "INV-000001" in page and "tag overdue" in page
    assert "INV-000001" not in adm.get("/sales/invoices/?status=VOID").content.decode()
    overdue = adm.get(f"/sales/invoices/?customer={customer.pk}&overdue=1").content.decode()
    assert old.number in overdue and recent.number not in overdue
    assert adm.get("/sales/orders/").status_code == 200

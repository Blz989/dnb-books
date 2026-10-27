import html
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from contacts.models import Vendor
from items.models import ItemType
from items.stock import on_hand
from purchasing.models import Bill, BillRevision, DropShipAllocation, PurchaseOrder
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, bill_from_po, create_bill
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import DocStatus, Fulfillment, OrderStatus
from sales.posting.invoices import invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order

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
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.5000", sale_price="2.0000")


@pytest.fixture
def service(setup, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     purchase_cost="12.0000")


def rows(*items, prefix="lines"):
    """items: (sku, qty, price[, description[, ref]])"""
    data = {}
    for i, (sku, qty, price, *rest) in enumerate(items):
        description, ref = (list(rest) + ["", ""])[:2]
        data.update({f"{prefix}-{i}-sku": sku, f"{prefix}-{i}-quantity": str(qty), f"{prefix}-{i}-price": price,
                     f"{prefix}-{i}-description": description, f"{prefix}-{i}-ref": ref})
    return data


def po_post(vendor, *items, token="tok-po-1", **extra):
    data = {"token": token, "vendor": vendor.pk, "date": "2026-10-05", "expected_date": "", "notes": "", **rows(*items)}
    data.update(extra)
    return data


def bill_post(vendor, *items, expense=(), token="tok-bill-1", **extra):
    data = {"token": token, "vendor": vendor.pk, "vendor_invoice_number": "V-100", "date": "2026-10-06", "terms": "",
            "notes": "", **rows(*items)}
    for i, (account, amount, description) in enumerate(expense):
        data.update({f"exp-{i}-account": str(account.pk), f"exp-{i}-amount": amount, f"exp-{i}-description": description})
    data.update(extra)
    return data


def text(response):
    return html.unescape(response.content.decode())


def test_pages_need_login(client, setup, vendor):
    for path in ("/purchasing/orders/", "/purchasing/orders/new/", "/purchasing/bills/", "/purchasing/bills/new/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


# ---- purchase orders ----

def test_create_po_and_see_it(adm, vendor, widget):
    r = adm.post("/purchasing/orders/new/", po_post(vendor, ("WIDGET", 1000, "0.50")))
    po = PurchaseOrder.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/purchasing/orders/{po.number}/"
    page = text(adm.get(r["Location"]))
    assert "PO-00001" in page and "Acme Supply" in page and "1000" in page
    assert "PO-00001" in text(adm.get("/purchasing/orders/"))


def test_price_defaults_to_the_item_purchase_cost(adm, vendor, widget):
    adm.post("/purchasing/orders/new/", po_post(vendor, ("WIDGET", 10, "")))
    assert PurchaseOrder.objects.get().lines.get().price == D("0.5000")


def test_po_form_messages(adm, vendor, widget):
    r = adm.post("/purchasing/orders/new/", po_post(vendor, ("NOPE", 1, "1.00")))
    assert "Line 1: no item has SKU NOPE." in text(r)
    r = adm.post("/purchasing/orders/new/", po_post(vendor))
    assert "A purchase order needs at least one line." in text(r)
    assert PurchaseOrder.objects.count() == 0


def test_double_submit_posts_once(adm, vendor, widget):
    data = po_post(vendor, ("WIDGET", 5, "0.50"))
    adm.post("/purchasing/orders/new/", data)
    r = adm.post("/purchasing/orders/new/", data, follow=True)
    assert PurchaseOrder.objects.count() == 1
    assert "This purchase order was already saved." in text(r)


def test_edit_po_and_close_reopen(adm, vendor, widget, admin_user):
    po = create_po(vendor=vendor, date=date(2026, 10, 1), user=admin_user, lines=[POLineSpec(widget, 10, D("0.50"))])
    line = po.lines.get()
    page = text(adm.get(f"/purchasing/orders/{po.number}/edit/"))
    assert f'value="{line.pk}"' in page
    r = adm.post(f"/purchasing/orders/{po.number}/edit/", po_post(vendor, ("WIDGET", 12, "0.55", "", str(line.pk))))
    assert r.status_code == 302
    line.refresh_from_db()
    assert (line.quantity, line.price) == (12, D("0.5500"))
    adm.post(f"/purchasing/orders/{po.number}/close/")
    po.refresh_from_db()
    assert po.status == OrderStatus.CLOSED
    assert "is closed. Reopen it to make changes." in text(
        adm.post(f"/purchasing/orders/{po.number}/edit/", po_post(vendor, ("WIDGET", 12, "0.55", "", str(line.pk)))))
    adm.post(f"/purchasing/orders/{po.number}/close/")
    po.refresh_from_db()
    assert po.status == OrderStatus.OPEN


def test_standard_user_can_enter_pos_and_bills(std, vendor, widget):
    assert std.post("/purchasing/orders/new/", po_post(vendor, ("WIDGET", 5, "0.50"))).status_code == 302
    assert std.post("/purchasing/bills/new/", bill_post(vendor, ("WIDGET", 5, "0.50"))).status_code == 302


# ---- bills ----

@pytest.fixture
def po(vendor, admin_user, widget):
    return create_po(vendor=vendor, date=date(2026, 10, 1), user=admin_user, lines=[POLineSpec(widget, 1000, D("0.50"))])


def test_bill_a_po_in_two_parts_through_the_views(adm, po, widget, freight, vendor):
    page = text(adm.get(f"/purchasing/orders/{po.number}/bill/"))
    assert 'value="1000"' in page and 'value="0.50"' in page
    line = po.lines.get()
    r = adm.post(f"/purchasing/orders/{po.number}/bill/", {
        "token": "t1", "date": "2026-10-06", "vendor_invoice_number": "V-1", "terms": "", "notes": "",
        f"qty-{line.pk}": "500", f"price-{line.pk}": "0.48", "exp-0-account": freight.pk, "exp-0-amount": "25.00",
        "exp-0-description": "Freight"})
    bill = Bill.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/purchasing/bills/{bill.number}/"
    assert on_hand(widget) == 500
    detail = text(adm.get(r["Location"]))
    assert "265.00" in detail and "Freight" in detail and "V-1" in detail and po.number in detail
    po.refresh_from_db()
    assert po.status == OrderStatus.OPEN
    r = adm.post(f"/purchasing/orders/{po.number}/bill/", {
        "token": "t2", "date": "2026-10-07", "vendor_invoice_number": "V-2", "terms": "", "notes": "",
        f"qty-{line.pk}": "500", f"price-{line.pk}": "0.50"})
    assert r.status_code == 302 and Bill.objects.count() == 2
    po.refresh_from_db()
    assert po.status == OrderStatus.CLOSED
    assert "is closed. Reopen it to bill it." in text(adm.get(f"/purchasing/orders/{po.number}/bill/", follow=True))


def test_over_receiving_shows_the_message(adm, po):
    line = po.lines.get()
    r = adm.post(f"/purchasing/orders/{po.number}/bill/", {
        "token": "t1", "date": "2026-10-06", "vendor_invoice_number": "V-1", "terms": "", "notes": "",
        f"qty-{line.pk}": "1001", f"price-{line.pk}": "0.50"})
    assert r.status_code == 200 and "Line 1: only 1000 left to receive on PO-00001." in text(r)
    assert Bill.objects.count() == 0


def test_new_bill_with_stock_expense_and_defaults(adm, vendor, widget, freight):
    r = adm.post("/purchasing/bills/new/", bill_post(vendor, ("WIDGET", 10, "0.50"), expense=[(freight, "5.00", "Freight")]))
    bill = Bill.objects.get()
    assert r.status_code == 302 and bill.terms == "NET_30" and bill.due_date == date(2026, 11, 5)
    assert on_hand(widget) == 10 and bill.expense_lines.get().account == freight


def test_bill_form_messages(adm, vendor, widget, freight):
    r = adm.post("/purchasing/bills/new/", bill_post(vendor))
    assert "A bill needs at least one line." in text(r)
    r = adm.post("/purchasing/bills/new/", bill_post(vendor, expense=[(freight, "abc", "")]))
    assert "Line 1: enter an amount above zero." in text(r)
    r = adm.post("/purchasing/bills/new/", bill_post(vendor, ("WIDGET", 1, "0.50"), date="not a date"))
    assert "Enter a date like 2026-01-31." in text(r)
    assert Bill.objects.count() == 0


def test_stock_line_without_inventory_settings(adm, vendor, make_item, purchasing_settings):
    item = make_item("RAW")
    r = adm.post("/purchasing/bills/new/", bill_post(vendor, ("RAW", 1, "1.00")))
    page = text(r)
    assert "Set up inventory accounts first." in page and "/items/settings/" in page
    assert Bill.objects.count() == 0


def test_duplicate_vendor_invoice_confirm_flow(adm, vendor, widget):
    adm.post("/purchasing/bills/new/", bill_post(vendor, ("WIDGET", 1, "1.00"), vendor_invoice_number="INV-88123"))
    second = bill_post(vendor, ("WIDGET", 1, "1.00"), token="tok-2", vendor_invoice_number="inv-88123 ")
    r = adm.post("/purchasing/bills/new/", second)
    page = text(r)
    assert "Acme Supply already has bill BILL-00001 with invoice # INV-88123. Save anyway?" in page
    assert "Save anyway" in page and Bill.objects.count() == 1
    r = adm.post("/purchasing/bills/new/", {**second, "allow_duplicate": "1"})
    assert r.status_code == 302 and Bill.objects.count() == 2


def test_duplicate_go_back_keeps_the_form(adm, vendor, widget):
    adm.post("/purchasing/bills/new/", bill_post(vendor, ("WIDGET", 1, "1.00"), vendor_invoice_number="A1"))
    r = adm.post("/purchasing/bills/new/", {**bill_post(vendor, ("WIDGET", 3, "1.00"), token="tok-3", vendor_invoice_number="a1"),
                                            "back": "1"})
    page = text(r)
    assert r.status_code == 200 and 'value="3"' in page and Bill.objects.count() == 1


def test_bill_double_submit_posts_once(adm, vendor, widget):
    data = bill_post(vendor, ("WIDGET", 5, "0.50"))
    adm.post("/purchasing/bills/new/", data)
    r = adm.post("/purchasing/bills/new/", data, follow=True)
    assert Bill.objects.count() == 1 and on_hand(widget) == 5
    assert "This bill was already saved." in text(r)


def test_bill_list_filters(adm, vendor, widget, admin_user, freight):
    a = create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="A", user=admin_user,
                    expense_lines=[BillExpenseSpec(freight, D("10.00"))])
    other = Vendor.objects.create(name="Zed Parts")
    create_bill(vendor=other, date=date(2026, 10, 5), vendor_invoice_number="B", user=admin_user,
                expense_lines=[BillExpenseSpec(freight, D("20.00"))])
    page = text(adm.get("/purchasing/bills/"))
    assert a.number in page and "Zed Parts" in page and "Overdue" in page
    only = text(adm.get(f"/purchasing/bills/?vendor={other.pk}"))
    assert "Zed Parts" in only and a.number not in only
    assert a.number in text(adm.get("/purchasing/bills/?q=acme"))


@pytest.fixture
def stock_bill(vendor, admin_user, widget, freight):
    return create_bill(vendor=vendor, date=date(2026, 10, 5), vendor_invoice_number="V-1", user=admin_user,
                       item_lines=[BillItemSpec(widget, 10, D("0.50"))],
                       expense_lines=[BillExpenseSpec(freight, D("25.00"), "Freight")])


def test_edit_creates_a_revision_you_can_open(adm, stock_bill, vendor, freight):
    page = text(adm.get(f"/purchasing/bills/{stock_bill.number}/edit/"))
    assert "Reason for the change" in page and "WIDGET" in page
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/edit/", bill_post(
        vendor, ("WIDGET", 10, "0.60"), expense=[(freight, "25.00", "Freight")], reason="Price was 0.60",
        token="tok-edit", vendor_invoice_number="V-1", date="2026-10-05"))
    assert r.status_code == 302
    stock_bill.refresh_from_db()
    assert stock_bill.revision == 2
    rev = BillRevision.objects.get()
    detail = text(adm.get(f"/purchasing/bills/{stock_bill.number}/"))
    assert "Revision 2" in detail and "Price was 0.60" in detail
    old = text(adm.get(f"/purchasing/bills/{stock_bill.number}/revisions/{rev.revision}/"))
    assert "0.5000" in old and "Freight" in old and "Read only" in old


def test_edit_negative_stock_confirm(adm, stock_bill, vendor, widget, admin_user, freight, customer):
    from sales.posting.invoices import InvoiceLineSpec, create_invoice

    create_invoice(customer=customer, date=date(2026, 10, 10), user=admin_user, lines=[InvoiceLineSpec(widget, 8, D("2.00"))])
    data = bill_post(vendor, ("WIDGET", 5, "0.50"), expense=[(freight, "25.00", "")], token="tok-neg",
                     vendor_invoice_number="V-1", date="2026-10-05")
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/edit/", data)
    page = text(r)
    assert "WIDGET has" in page and "Continue?" in page and "Save anyway" in page
    stock_bill.refresh_from_db()
    assert stock_bill.revision == 1
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/edit/", {**data, "allow_negative": "1"})
    assert r.status_code == 302 and on_hand(widget) == -3


def test_void_is_admin_only_and_needs_a_reason(adm, std, stock_bill):
    assert std.get(f"/purchasing/bills/{stock_bill.number}/void/").status_code == 403
    assert std.post(f"/purchasing/bills/{stock_bill.number}/void/", {"date": "2026-10-05", "reason": "x"}).status_code == 403
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/void/", {"date": "2026-10-05", "reason": ""})
    assert "Enter a reason." in text(r)
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/void/", {"date": "2026-10-05", "reason": "Entered twice"})
    assert r.status_code == 302
    stock_bill.refresh_from_db()
    assert stock_bill.status == DocStatus.VOID
    assert "Entered twice" in text(adm.get(f"/purchasing/bills/{stock_bill.number}/"))


def test_void_with_applications_message(adm, stock_bill, admin_user, cash, vendor):
    from purchasing.models import VendorPaymentMethod as M
    from purchasing.posting.payments import PayLine, pay_bills

    pay_bills(date=date(2026, 10, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(stock_bill, D("10.00"))])
    r = adm.post(f"/purchasing/bills/{stock_bill.number}/void/", {"date": "2026-10-20", "reason": "x"})
    assert "BILL-00001 has payments or credits applied. Remove them first." in text(r)
    page = text(adm.get(f"/purchasing/bills/{stock_bill.number}/"))
    assert "VP-00001" in page
    app = stock_bill.payment_applications.get()
    adm.post(f"/purchasing/bills/{stock_bill.number}/applications/payment/{app.pk}/remove/")
    app.refresh_from_db()
    assert app.removed


# ---- drop ship ----

@pytest.fixture
def sales_order(customer, admin_user, widget):
    return create_order(customer=customer, date=date(2026, 10, 1), user=admin_user, lines=[
        OrderLineSpec(widget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Delta Faucet"),
        OrderLineSpec(widget, 2, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Supply")])


def test_sales_order_shows_the_button_only_when_lines_need_a_po(adm, sales_order, vendor, admin_user):
    page = text(adm.get(f"/sales/orders/{sales_order.number}/"))
    assert "Create drop-ship PO" in page and f"/purchasing/orders/drop-ship/{sales_order.number}/" in page
    Vendor.objects.create(name="Delta Faucet")
    r = adm.post(f"/purchasing/orders/drop-ship/{sales_order.number}/", {})
    assert r.status_code == 302
    after = text(adm.get(f"/sales/orders/{sales_order.number}/"))
    assert "Create drop-ship PO" not in after and "PO-00001" in after


def test_drop_ship_po_page_lists_vendor_names_and_asks_for_unmatched(adm, sales_order, vendor):
    page = text(adm.get(f"/purchasing/orders/drop-ship/{sales_order.number}/"))
    assert "Delta Faucet" in page and "Acme Supply" in page and "No vendor named Delta Faucet" in page
    assert "<select" in page


def test_unmatched_drop_ship_vendor_then_choose_one(adm, sales_order, vendor):
    other = Vendor.objects.create(name="Delta Faucet Supply Inc")
    r = adm.post(f"/purchasing/orders/drop-ship/{sales_order.number}/", {})
    assert r.status_code == 200 and "No vendor named Delta Faucet. Choose a vendor for these lines." in text(r)
    assert PurchaseOrder.objects.count() == 0
    r = adm.post(f"/purchasing/orders/drop-ship/{sales_order.number}/", {"vendor-Delta Faucet": other.pk})
    assert r.status_code == 302
    assert sorted(p.vendor.name for p in PurchaseOrder.objects.all()) == ["Acme Supply", "Delta Faucet Supply Inc"]


def test_bill_detail_links_drop_ship_allocations_to_invoices(adm, customer, admin_user, widget, vendor):
    vendor.name = "Delta Faucet"
    vendor.save()
    order = create_order(customer=customer, date=date(2026, 10, 1), user=admin_user, lines=[
        OrderLineSpec(widget, 3, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Delta Faucet")])
    (drop_po,) = create_drop_ship_pos(order, user=admin_user, date=date(2026, 10, 2))
    invoice = invoice_from_order(order, date=date(2026, 10, 3), quantities={order.lines.get().pk: 3}, user=admin_user)
    bill = bill_from_po(drop_po, date=date(2026, 10, 4), vendor_invoice_number="DS", user=admin_user,
                        quantities={drop_po.lines.get().pk: 3})
    page = text(adm.get(f"/purchasing/bills/{bill.number}/"))
    assert f"/sales/invoices/{invoice.number}/" in page and "Drop-ship allocations" in page
    assert DropShipAllocation.objects.count() == 1
    po_page = text(adm.get(f"/purchasing/orders/{drop_po.number}/"))
    assert order.number in po_page

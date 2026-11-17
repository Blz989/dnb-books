"""Void, then delete: invoices, bills, sales orders and purchase orders leave the lists and stay on record."""
from datetime import date
from decimal import Decimal

import pytest
from auditlog.models import LogEntry
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from contacts.models import Vendor
from items.stock import on_hand
from ledger.models import JournalEntry
from purchasing.errors import PurchasingError
from purchasing.models import PurchaseOrder, PurchaseOrderAllocation
from purchasing.posting.bills import bill_from_po, void_bill
from purchasing.posting.deletion import delete_bill, delete_po, delete_sales_order
from purchasing.posting.orders import create_drop_ship_pos, drop_ship_candidates
from sales.errors import InvoiceError, OrderError
from sales.models import DocStatus, Fulfillment, OrderStatus
from sales.posting.deletion import delete_invoice
from sales.posting.invoices import invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order, reopen_order, update_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", sale_price="5.0000")


def so_with_drop_ship(customer, admin_user, gadget, qty=3, day=1):
    return create_order(customer=customer, date=d(10, day), user=admin_user, lines=[
        OrderLineSpec(gadget, qty, D("9.50"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])


@pytest.fixture
def order(customer, admin_user, gadget):
    return so_with_drop_ship(customer, admin_user, gadget)


@pytest.fixture
def po(order, admin_user):
    (po,) = create_drop_ship_pos(order, user=admin_user, date=d(10, 2))
    return po


def bill(po, admin_user, qty=3):
    line = po.lines.get()
    return bill_from_po(po, date=d(10, 8), vendor_invoice_number="V-1", quantities={line.pk: qty}, user=admin_user)


def invoice(order, admin_user, qty=3):
    line = order.lines.get()
    return invoice_from_order(order, date=d(10, 5), quantities={line.pk: qty}, user=admin_user)


# ---- invoices and bills ----

def test_open_invoice_cannot_be_deleted(order, admin_user):
    inv = invoice(order, admin_user)
    with pytest.raises(InvoiceError, match="Void this invoice first"):
        delete_invoice(inv, user=admin_user)


def test_void_invoice_delete_keeps_ledger_and_audit_trail(order, admin_user):
    inv = invoice(order, admin_user)
    void_invoice(inv, user=admin_user, reason="Mistake")
    entries = JournalEntry.objects.count()
    delete_invoice(inv, user=admin_user)
    inv.refresh_from_db()
    assert inv.deleted_at and inv.deleted_by == admin_user and inv.status == DocStatus.VOID
    assert JournalEntry.objects.count() == entries  # nothing posted, nothing removed
    log = LogEntry.objects.filter(object_pk=str(inv.pk), content_type__model="invoice")
    assert any("deleted_at" in e.changes_dict for e in log)
    with pytest.raises(InvoiceError, match="already deleted"):
        delete_invoice(inv, user=admin_user)


def test_only_admin_can_delete(order, admin_user, standard_user):
    inv = invoice(order, admin_user)
    void_invoice(inv, user=admin_user, reason="Mistake")
    with pytest.raises(PermissionDenied):
        delete_invoice(inv, user=standard_user)


def test_bill_delete_needs_void_first(po, admin_user):
    b = bill(po, admin_user)
    with pytest.raises(PurchasingError, match="Void this bill first"):
        delete_bill(b, user=admin_user)
    void_bill(b, user=admin_user, reason="Wrong")
    delete_bill(b, user=admin_user)
    b.refresh_from_db()
    assert b.deleted_at and b.status == DocStatus.VOID


# ---- sales orders ----

def test_so_with_open_invoice_is_blocked(order, admin_user):
    invoice(order, admin_user)
    with pytest.raises(OrderError) as exc:
        delete_sales_order(order, user=admin_user)
    assert str(exc.value) == "Void the invoices on this order first."


def test_so_delete_after_void_removes_sole_po_and_frees_nothing_else(order, po, admin_user):
    inv = invoice(order, admin_user)
    void_invoice(inv, user=admin_user, reason="Mistake")
    delete_sales_order(order, user=admin_user)
    order.refresh_from_db()
    po.refresh_from_db()
    assert order.deleted_at and order.status == OrderStatus.CLOSED
    assert po.deleted_at and po.status == OrderStatus.CLOSED
    assert not PurchaseOrderAllocation.objects.exists()
    assert drop_ship_candidates(order) == {}
    with pytest.raises(OrderError, match="was deleted"):
        reopen_order(order, admin_user)
    with pytest.raises(OrderError, match="was deleted"):
        update_order(order, lines=[OrderLineSpec(order.lines.get().item, 1, D("1.00"))], user=admin_user)


def test_so_blocked_by_open_bill_on_its_po(order, po, admin_user):
    bill(po, admin_user)
    with pytest.raises(OrderError, match=f"Void the bills on purchase order {po.number} first"):
        delete_sales_order(order, user=admin_user)
    b = po.bills.get()
    void_bill(b, user=admin_user, reason="Wrong")
    delete_sales_order(order, user=admin_user)  # a voided bill no longer blocks
    order.refresh_from_db()
    assert order.deleted_at


def test_so_delete_on_shared_po_only_releases_its_share(customer, admin_user, gadget):
    one = so_with_drop_ship(customer, admin_user, gadget, qty=3, day=1)
    two = so_with_drop_ship(customer, admin_user, gadget, qty=2, day=2)
    (po,) = create_drop_ship_pos([one, two], user=admin_user, date=d(10, 3))
    assert po.lines.get().quantity == 5
    delete_sales_order(one, user=admin_user)
    po.refresh_from_db()
    line = po.lines.get()
    assert po.deleted_at is None and line.quantity == 2
    (alloc,) = line.allocations.all()
    assert alloc.so_line.order == two and alloc.quantity == 2


def test_so_delete_leaves_stock_alone(customer, admin_user, widget):
    from items.models import MovementKind
    from items.stock import StockLine, record_movements

    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user,
                     lines=[StockLine(widget, 10, unit_cost=D("2.00"))])
    so = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(widget, 4, D("5.00"))])
    inv = invoice_from_order(so, date=d(10, 3), quantities={so.lines.get().pk: 4}, user=admin_user)
    assert on_hand(widget) == 6
    void_invoice(inv, user=admin_user, reason="Mistake")
    assert on_hand(widget) == 10  # the void puts the stock back
    delete_sales_order(so, user=admin_user)
    assert on_hand(widget) == 10  # deleting changes nothing more


# ---- purchase orders ----

def test_po_with_open_bill_is_blocked(po, admin_user):
    bill(po, admin_user)
    with pytest.raises(PurchasingError) as exc:
        delete_po(po, user=admin_user)
    assert str(exc.value) == "Void the bills on this purchase order first."


def test_po_delete_frees_the_sales_order_lines(order, po, admin_user):
    delete_po(po, user=admin_user)
    po.refresh_from_db()
    assert po.deleted_at and po.status == OrderStatus.CLOSED
    assert not PurchaseOrderAllocation.objects.exists()
    assert [l.pk for l in drop_ship_candidates(order)["Acme Mfg"]] == [order.lines.get().pk]
    (again,) = create_drop_ship_pos(order, user=admin_user, date=d(10, 9))
    assert again.pk != po.pk


def test_po_with_voided_bill_can_be_deleted(po, admin_user):
    b = bill(po, admin_user)
    void_bill(b, user=admin_user, reason="Wrong")
    delete_po(po, user=admin_user)
    po.refresh_from_db()
    assert po.deleted_at


# ---- screens ----

def test_confirm_screens_and_lists(client, admin_user, order, po):
    client.force_login(admin_user)
    r = client.get(reverse("sales:order-delete", args=[order.number]))
    assert r.status_code == 200 and f"{po.number}" in r.content.decode() and "deleted too" in r.content.decode()
    r = client.post(reverse("sales:order-delete", args=[order.number]))
    assert r.status_code == 302 and r.url == reverse("sales:order-list")
    client.get(reverse("sales:order-list"))  # shows the "was deleted" message once
    assert order.number not in client.get(reverse("sales:order-list")).content.decode()
    assert po.number not in client.get(reverse("purchasing:po-list")).content.decode()
    detail = client.get(reverse("sales:order-detail", args=[order.number]))
    assert detail.status_code == 200 and "Deleted on" in detail.content.decode()
    assert client.get(reverse("sales:order-edit", args=[order.number])).status_code == 302
    assert client.post(reverse("sales:order-close", args=[order.number])).status_code == 302


def test_blocked_confirm_screen_says_why(client, admin_user, order):
    invoice(order, admin_user)
    client.force_login(admin_user)
    r = client.get(reverse("sales:order-delete", args=[order.number]))
    assert "Void the invoices on this order first." in r.content.decode()
    r = client.post(reverse("sales:order-delete", args=[order.number]))
    assert r.status_code == 200 and not order.__class__.objects.get(pk=order.pk).deleted_at


def test_invoice_and_bill_delete_views(client, admin_user, order, po):
    client.force_login(admin_user)
    inv = invoice(order, admin_user)
    r = client.get(reverse("sales:invoice-delete", args=[inv.number]))
    assert "Void this invoice first" in r.content.decode()
    void_invoice(inv, user=admin_user, reason="Mistake")
    assert client.post(reverse("sales:invoice-delete", args=[inv.number])).status_code == 302
    client.get(reverse("sales:invoice-list"))
    assert inv.number not in client.get(reverse("sales:invoice-list")).content.decode()
    b = bill(po, admin_user)
    assert "Void this bill first" in client.get(reverse("purchasing:bill-delete", args=[b.number])).content.decode()
    void_bill(b, user=admin_user, reason="Wrong")
    assert client.post(reverse("purchasing:bill-delete", args=[b.number])).status_code == 302
    client.get(reverse("purchasing:bill-list"))
    assert b.number not in client.get(reverse("purchasing:bill-list")).content.decode()
    r = client.get(reverse("purchasing:po-delete", args=[po.number]))
    assert r.status_code == 200 and "Delete purchase order" in r.content.decode()


def test_standard_user_cannot_open_delete_screens(client, standard_user, order, po):
    client.force_login(standard_user)
    assert client.get(reverse("sales:order-delete", args=[order.number])).status_code == 403
    assert client.get(reverse("purchasing:po-delete", args=[po.number])).status_code == 403


# ---- imported numbers with a slash ----

def test_numbers_with_a_slash_list_and_open(client, admin_user, order, po):
    """Zoho numbers like "GF-Net Credit 6/2025" must not break lists or routes."""
    from purchasing.models import Bill, PurchaseOrder, VendorCredit
    from sales.models import Invoice, SalesOrder

    inv = invoice(order, admin_user)
    b = bill(po, admin_user)
    for model, old, new in ((Invoice, inv.number, "INV 6/2025"), (Bill, b.number, "GF-Net Bill 6/2025"),
                            (PurchaseOrder, po.number, "PO 6/2025"), (SalesOrder, order.number, "SO 6/2025")):
        model.objects.filter(number=old).update(number=new)
    client.force_login(admin_user)
    for name, number in (("sales:invoice-detail", "INV 6/2025"), ("purchasing:bill-detail", "GF-Net Bill 6/2025"),
                         ("purchasing:po-detail", "PO 6/2025"), ("sales:order-detail", "SO 6/2025")):
        url = reverse(name, args=[number])
        assert "~s~" in url and "/" not in url.rsplit("/", 2)[-2]
        assert client.get(url).status_code == 200
    for name in ("sales:invoice-list", "purchasing:bill-list", "purchasing:po-list", "sales:order-list"):
        page = client.get(reverse(name))
        assert page.status_code == 200
    assert client.get(reverse("sales:order-delete", args=["SO 6/2025"])).status_code == 200
    assert client.get(reverse("sales:order-edit", args=["SO 6/2025"])).status_code == 200


def test_vendor_credit_number_with_a_slash(client, admin_user, setup):
    from purchasing.models import VendorCredit, VendorCreditKind

    vendor = Vendor.objects.get(name="Acme Mfg")
    credit = VendorCredit.objects.create(
        number="GF-Net Credit 6/2025", vendor=vendor, date=d(6, 1), kind=VendorCreditKind.ALLOWANCE,
        reason="x", created_by=admin_user)
    client.force_login(admin_user)
    page = client.get(reverse("purchasing:credit-list"))
    assert page.status_code == 200 and "GF-Net Credit 6/2025" in page.content.decode()
    assert client.get(reverse("purchasing:credit-detail", args=[credit.number])).status_code == 200

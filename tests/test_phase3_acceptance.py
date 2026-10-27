"""Spec section 1: a scripted run through the screens, with the AR and Undeposited checks at zero after every step."""
import html
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client

from contacts.models import Customer
from items.models import ItemType, MovementKind
from items.stock import StockLine, record_movements
from sales.balances import credit_unapplied, invoice_balance, payment_unapplied
from sales.checks import ar_check, undeposited_check
from sales.models import (
    CreditNote, Deposit, DocStatus, Invoice, OrderStatus, Payment, PaymentMethod, Refund, SalesOrder,
)

D = Decimal


def text(response):
    return html.unescape(response.content.decode())


@pytest.fixture
def world(inv_settings, sales_settings, admin_user, make_item, bank):
    bolt = make_item("BOLT", sale_price="0.8500")
    record_movements(kind=MovementKind.OPENING, date=date(2026, 10, 1), user=admin_user,
                     lines=[StockLine(bolt, 100, unit_cost=D("0.50"))])
    pump = make_item("PUMP", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     sale_price="1240.00")
    client = Client()
    client.force_login(admin_user)
    return client, bolt, pump, Customer.objects.create(name="Acme Builders", ship_to_address="Dock 4")


def checks():
    assert ar_check() == [] and undeposited_check() == []
    for day in range(1, 31):
        assert ar_check(date(2026, 10, day)) == [] and undeposited_check(date(2026, 10, day)) == []


def row(i, sku, qty, price="", fulfillment="STOCK", vendor="", ref=""):
    return {f"lines-{i}-sku": sku, f"lines-{i}-quantity": str(qty), f"lines-{i}-price": price,
            f"lines-{i}-fulfillment": fulfillment, f"lines-{i}-vendor": vendor, f"lines-{i}-ref": ref}


def test_phase3_scripted_run(world, bank):
    client, bolt, pump, customer = world
    checks()

    # 1. A sales order with a stock line and a drop-ship line.
    r = client.post("/sales/orders/new/", {
        "token": "a1", "customer": customer.pk, "date": "2026-10-02", "ship_to": "", "notes": "",
        **row(0, "BOLT", 100), **row(1, "PUMP", 1, "1240.00", "DROP_SHIP", "Acme Mfg")})
    assert r.status_code == 302
    order = SalesOrder.objects.get()
    stock_line, drop_line = order.lines.all()
    checks()

    # 2. Invoice it in two shipments, the second including the drop-ship line.
    r = client.post(f"/sales/orders/{order.number}/invoice/", {
        "token": "a2", "date": "2026-10-05", "terms": "", "ship_to": "Dock 4", "notes": "",
        f"qty-{stock_line.pk}": "60", f"qty-{drop_line.pk}": "0"})
    assert r.status_code == 302
    r = client.post(f"/sales/orders/{order.number}/invoice/", {
        "token": "a3", "date": "2026-10-12", "terms": "", "ship_to": "Dock 4", "notes": "",
        f"qty-{stock_line.pk}": "40", f"qty-{drop_line.pk}": "1"})
    assert r.status_code == 302
    first, second = Invoice.objects.order_by("id")
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED and second.lines.filter(fulfillment="DROP_SHIP").count() == 1
    checks()

    # 3. Edit the first invoice's quantity.
    r = client.post(f"/sales/invoices/{first.number}/edit/", {
        "token": "a4", "customer": customer.pk, "date": "2026-10-05", "terms": "NET_30", "ship_to": "Dock 4", "notes": "",
        "reason": "Customer took 55", **row(0, "BOLT", 55, "0.85", ref=str(stock_line.pk))})
    assert r.status_code == 302
    first.refresh_from_db()
    assert first.revision == 2 and invoice_balance(first) == D("46.75")
    checks()

    # 4. A check payment and an ACH payment.
    r = client.post("/sales/payments/new/", {
        "token": "a5", "customer": customer.pk, "date": "2026-10-14", "method": "CHECK", "reference": "4410",
        "amount": "46.75", f"apply-{first.pk}": "46.75"})
    assert r.status_code == 302
    r = client.post("/sales/payments/new/", {
        "token": "a6", "customer": customer.pk, "date": "2026-10-15", "method": "ACH", "bank_account": bank.pk,
        "amount": "1300.00", f"apply-{second.pk}": "1254.00"})
    assert r.status_code == 302
    check_pmt, ach_pmt = Payment.objects.order_by("id")
    assert payment_unapplied(ach_pmt) == D("46.00") and invoice_balance(second) == D("20.00")
    checks()

    # 5. Deposit the check.
    r = client.post("/sales/deposits/new/", {"token": "a7", "date": "2026-10-16", "bank_account": bank.pk, "payments": [check_pmt.pk]})
    assert r.status_code == 302 and Deposit.objects.count() == 1
    checks()

    # 6. A credit note, applied.
    r = client.post("/sales/credits/new/", {
        "token": "a8", "customer": customer.pk, "date": "2026-10-17", "reason": "Pump gasket",
        "lines-0-description": "Replacement gasket", "lines-0-amount": "20.00"})
    assert r.status_code == 302
    note = CreditNote.objects.get()
    r = client.post(f"/sales/credits/{note.number}/apply/", {"token": "a9", "date": "2026-10-18", f"apply-{second.pk}": "20.00"})
    assert r.status_code == 302
    assert invoice_balance(second) == D("0.00") and credit_unapplied(note) == D("0.00")
    checks()

    # 7. Refund the overpayment (the ACH left 46.00 unapplied).
    r = client.post("/sales/refunds/new/", {
        "token": "a10", "source_payment": ach_pmt.number, "date": "2026-10-19", "method": "ACH", "bank_account": bank.pk, "amount": "46.00"})
    assert r.status_code == 302 and Refund.objects.count() == 1
    assert payment_unapplied(ach_pmt) == D("0.00")
    checks()

    # The invoice PDF.
    pdf = client.get(f"/sales/invoices/{first.number}/pdf/")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf" and pdf.content.startswith(b"%PDF")


def test_home_cards_show_the_sales_numbers(world, admin_user, bank):
    from sales.posting.invoices import InvoiceLineSpec, create_invoice
    from sales.posting.receipts import receive_payment

    from django.utils import timezone

    today = timezone.localdate()
    client, bolt, pump, customer = world
    create_invoice(customer=customer, date=today - timedelta(days=120), user=admin_user, lines=[InvoiceLineSpec(pump, 1, D("200.00"))])
    create_invoice(customer=customer, date=today, user=admin_user, lines=[
        InvoiceLineSpec(pump, 1, D("50.00"), fulfillment="DROP_SHIP", drop_ship_vendor="Acme")])
    receive_payment(customer=customer, date=today, method=PaymentMethod.CHECK, amount=D("30.00"), user=admin_user)
    page = text(client.get("/"))
    assert "Open A/R" in page and "220.00" in page          # 200 + 50 owed, less the 30.00 unapplied check
    assert "Overdue" in page and ">200.00<" in page
    assert "1 overdue invoice" in page and "/reports/open-invoices/?overdue=1" in page
    assert "Undeposited" in page and ">30.00<" in page
    assert "Drop shipments awaiting bill" in page and ">1<" in page
    assert "/reports/drop-ship-awaiting-bill/" in page and "/reports/ar-aging/" in page and "/sales/deposits/new/" in page


def test_overdue_link_filters_the_open_invoices_report(world, admin_user):
    from sales.posting.invoices import InvoiceLineSpec, create_invoice

    from django.utils import timezone

    client, bolt, pump, customer = world
    create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(pump, 1, D("200.00"))])
    create_invoice(customer=customer, date=timezone.localdate(), user=admin_user, lines=[InvoiceLineSpec(pump, 1, D("50.00"))])
    page = text(client.get("/reports/open-invoices/?overdue=1"))
    assert "INV-000001" in page and "INV-000002" not in page


def test_unconfigured_sales_banner_for_admin(db, admin_user, client):
    client.force_login(admin_user)
    assert "Sales isn't set up yet." in text(client.get("/"))

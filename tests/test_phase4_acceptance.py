"""Spec section 1: a scripted run through the screens, with the A/P, A/R, inventory, and Undeposited Funds checks at
zero after every step, then the 1099 summary."""
import csv
import html
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from contacts.models import Customer, Terms, Vendor
from items.models import ItemType
from items.stock import check_invariants, on_hand
from purchasing.balances import bill_balance, vendor_credit_unapplied, vendor_payment_unapplied
from purchasing.checks import ap_check
from purchasing.models import Bill, PurchaseOrder, VendorCredit, VendorPayment
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import PayLine, pay_bills
from purchasing.reports import summary_1099
from sales.checks import ar_check, undeposited_check
from sales.models import Invoice, SalesOrder

D = Decimal


def text(response):
    return html.unescape(response.content.decode())


def checks():
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []
    for day in range(1, 32):
        as_of = date(2026, 10, day)
        assert ap_check(as_of) == [] and ar_check(as_of) == [] and undeposited_check(as_of) == [], as_of


def item_row(i, sku, qty, price="", ref="", prefix="lines"):
    return {f"{prefix}-{i}-sku": sku, f"{prefix}-{i}-quantity": str(qty), f"{prefix}-{i}-price": price,
            f"{prefix}-{i}-description": "", f"{prefix}-{i}-ref": ref}


@pytest.fixture
def world(inv_settings, purchasing_settings, sales_settings, admin_user, make_item, bank, card, freight, expense):
    bolt = make_item("BOLT", sale_price="0.8500", purchase_cost="0.5000")
    pump = make_item("PUMP", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     sale_price="1240.00", purchase_cost="900.0000")
    acme = Vendor.objects.create(name="Acme Supply", terms=Terms.NET_30, is_1099=True, tax_id="12-3456789")
    delta = Vendor.objects.create(name="Delta Pumps", terms=Terms.NET_30, is_1099=True, tax_id="98-7654321")
    landlord = Vendor.objects.create(name="Springfield Properties", is_1099=True, tax_id="55-5555555",
                                     default_expense_account=expense)
    customer = Customer.objects.create(name="Acme Builders", ship_to_address="Dock 4")
    client = Client()
    client.force_login(admin_user)
    return client, bolt, pump, acme, delta, landlord, customer


def test_phase4_scripted_run(world, bank, card, freight, expense):
    client, bolt, pump, acme, delta, landlord, customer = world
    checks()

    # 1. A purchase order for stock.
    r = client.post("/purchasing/orders/new/", {
        "token": "p1", "vendor": acme.pk, "date": "2026-10-01", "expected_date": "", "notes": "",
        **item_row(0, "BOLT", 1000, "0.50")})
    assert r.status_code == 302
    po = PurchaseOrder.objects.get()
    po_line = po.lines.get()
    checks()

    # 2. Receive and bill it in two parts; stock comes in at each bill's price.
    r = client.post(f"/purchasing/orders/{po.number}/bill/", {
        "token": "p2", "date": "2026-10-05", "vendor_invoice_number": "AS-100", "terms": "", "notes": "",
        f"qty-{po_line.pk}": "600", f"price-{po_line.pk}": "0.48", "exp-0-account": freight.pk,
        "exp-0-amount": "25.00", "exp-0-description": "Freight"})
    assert r.status_code == 302
    r = client.post(f"/purchasing/orders/{po.number}/bill/", {
        "token": "p3", "date": "2026-10-09", "vendor_invoice_number": "AS-117", "terms": "", "notes": "",
        f"qty-{po_line.pk}": "400", f"price-{po_line.pk}": "0.50"})
    assert r.status_code == 302
    first, second = Bill.objects.order_by("id")
    po.refresh_from_db()
    assert po.status == "CLOSED" and on_hand(bolt) == 1000
    assert (first.due_date, bill_balance(first), bill_balance(second)) == (date(2026, 11, 4), D("313.00"), D("200.00"))
    checks()

    # 3. A sales order with a drop-ship line, invoiced; its drop-ship PO; the vendor bill clears "awaiting bill".
    r = client.post("/sales/orders/new/", {
        "token": "s1", "customer": customer.pk, "date": "2026-10-10", "ship_to": "", "notes": "",
        "lines-0-sku": "PUMP", "lines-0-quantity": "1", "lines-0-price": "1240.00", "lines-0-fulfillment": "DROP_SHIP",
        "lines-0-vendor": "delta  pumps", "lines-0-description": "", "lines-0-ref": ""})
    assert r.status_code == 302
    order = SalesOrder.objects.get()
    drop_line = order.lines.get()
    r = client.post(f"/sales/orders/{order.number}/invoice/", {
        "token": "s2", "date": "2026-10-11", "terms": "", "ship_to": "Dock 4", "notes": "", f"qty-{drop_line.pk}": "1"})
    assert r.status_code == 302
    invoice = Invoice.objects.get()
    checks()
    page = text(client.get("/reports/drop-ship-awaiting-bill/"))
    assert invoice.number in page
    r = client.post(f"/purchasing/orders/drop-ship/{order.number}/", {})
    assert r.status_code == 302
    drop_po = PurchaseOrder.objects.exclude(pk=po.pk).get()
    assert drop_po.vendor == delta
    checks()
    r = client.post(f"/purchasing/orders/{drop_po.number}/bill/", {
        "token": "p4", "date": "2026-10-12", "vendor_invoice_number": "DP-5", "terms": "", "notes": "",
        f"qty-{drop_po.lines.get().pk}": "1", f"price-{drop_po.lines.get().pk}": "900.00"})
    assert r.status_code == 302
    drop_bill = Bill.objects.get(vendor=delta)
    assert on_hand(bolt) == 1000                      # a drop shipment never touches stock
    assert invoice.lines.get().drop_ship_billed is True
    assert invoice.number not in text(client.get("/reports/drop-ship-awaiting-bill/"))
    checks()

    # 4. An expense bill.
    r = client.post("/purchasing/bills/new/", {
        "token": "p5", "vendor": landlord.pk, "vendor_invoice_number": "RENT-OCT", "date": "2026-10-13", "terms": "",
        "notes": "", "exp-0-account": expense.pk, "exp-0-amount": "1500.00", "exp-0-description": "October rent"})
    assert r.status_code == 302
    rent = Bill.objects.get(vendor=landlord)
    checks()

    # 5. Pay three bills: a check, an ACH payment (partial), and a company card payment.
    def pay(token, bill, amount, method, account, **extra):
        r = client.post("/purchasing/pay/", {
            "token": token, "date": "2026-10-20", "method": method, "paid_from": account.pk, "vendor": "", "through": "",
            f"pay-{bill.pk}": amount, **extra})
        assert r.status_code == 302, text(r)

    pay("q1", first, "313.00", "CHECK", bank, **{f"check-{acme.pk}": "2001"})
    pay("q2", drop_bill, "500.00", "ACH", bank)
    pay("q3", rent, "1500.00", "CARD", card)
    assert bill_balance(first) == D("0.00") and bill_balance(drop_bill) == D("400.00") and bill_balance(rent) == D("0.00")
    assert VendorPayment.objects.count() == 3
    checks()

    # 6. Return stock to the vendor for a credit, and apply the credit to a bill.
    r = client.post("/purchasing/credits/new/?kind=return", {
        "token": "c1", "vendor": acme.pk, "date": "2026-10-22", "reason": "Wrong plating",
        "ret-0-sku": "BOLT", "ret-0-quantity": "50", "ret-0-amount": "30.00"})
    assert r.status_code == 302
    credit = VendorCredit.objects.get()
    assert on_hand(bolt) == 950 and vendor_credit_unapplied(credit) == D("30.00")
    r = client.post(f"/purchasing/credits/{credit.number}/apply/", {
        "token": "c2", "date": "2026-10-23", f"apply-{second.pk}": "30.00"})
    assert r.status_code == 302
    assert bill_balance(second) == D("170.00") and vendor_credit_unapplied(credit) == D("0.00")
    checks()

    # The 1099 summary: check and ACH only, the card payment left out, 2026's 2,000.00 threshold.
    summary = summary_1099(2026)
    assert summary.threshold == D("2000.00")
    assert [(r.vendor.name, r.total, r.file) for r in summary.rows] == [
        ("Acme Supply", D("313.00"), False), ("Delta Pumps", D("500.00"), False)]
    page = text(client.get("/reports/1099-summary/?year=2026"))
    assert "Acme Supply" in page and "Springfield Properties" not in page and "•••-••-6789" in page
    rows = list(csv.reader(io.StringIO(client.get("/reports/1099-summary/?year=2026&format=csv").content.decode())))
    assert rows[1][:2] == ["Acme Supply", "12-3456789"] and rows[1][3] == "313.00"
    # A/P aging ties out to the A/P account too.
    aging = text(client.get("/reports/ap-aging/?as_of=2026-10-31"))
    assert "Difference" in aging and "0.00" in aging


def test_home_cards_show_bills_due_and_overdue(world, admin_user):
    client, _, _, acme, _, landlord, _ = world
    today = timezone.localdate()
    from ledger.models import Account

    account = Account.objects.get(number="6000")
    spec = [BillExpenseSpec(account, D("100.00"))]
    overdue = create_bill(vendor=acme, date=today - timedelta(days=45), vendor_invoice_number="O", user=admin_user,
                          terms=Terms.NET_30, expense_lines=spec)
    soon = create_bill(vendor=acme, date=today - timedelta(days=12), vendor_invoice_number="S", user=admin_user,
                       terms=Terms.NET_15, expense_lines=[BillExpenseSpec(account, D("200.00"))])
    later = create_bill(vendor=landlord, date=today, vendor_invoice_number="L", user=admin_user,
                        terms=Terms.NET_60, expense_lines=[BillExpenseSpec(account, D("400.00"))])
    pay_bills(date=today, method="ACH", paid_from=Account.objects.get(number="1000"), user=admin_user,
              lines=[PayLine(soon, D("50.00"))])
    page = text(client.get("/"))
    assert "Bills due in 7 days" in page and ">150.00<" in page
    assert "Overdue bills" in page and ">100.00<" in page
    assert "1 overdue bill" in page and "/purchasing/bills/?overdue=1" in page
    assert f"/reports/bills-due/?through={(today + timedelta(days=7)).isoformat()}" in page
    assert later.number not in page


def test_unconfigured_purchasing_banner_for_admin(db, admin_user, client):
    client.force_login(admin_user)
    assert "Purchasing isn't set up yet." in text(client.get("/"))

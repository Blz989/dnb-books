import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from contacts.models import Customer, Terms
from items.models import ItemType
from ledger.balances import account_totals
from sales.models import Fulfillment, PaymentMethod
from sales.posting.deposits import make_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import (
    CreditLineSpec, apply_payment, create_credit_note, receive_payment, remove_application,
)
from sales.reports import (
    ar_aging, bucket_for, customer_balances, drop_ship_awaiting_bill, open_invoices, open_sales_orders,
    undeposited_funds,
)

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def invoice(service, customer, admin_user):
    # dated Oct 1, net 30: due Oct 31
    return create_invoice(customer=customer, date=d(10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("100.00"))])


@pytest.mark.parametrize("as_of, bucket", [
    (d(10, 30), "current"), (d(10, 31), "current"), (d(11, 1), "d1_30"), (d(11, 30), "d1_30"),
    (d(12, 1), "d31_60"), (d(12, 30), "d31_60"), (d(12, 31), "d61_90"), (date(2027, 1, 29), "d61_90"),
    (date(2027, 1, 30), "over_90"),
])
def test_bucket_edges(invoice, as_of, bucket):
    report = ar_aging(as_of)
    (row,) = report.rows
    assert getattr(row, bucket) == D("100.00")
    assert row.total == D("100.00")


def test_bucket_function_edges():
    assert [bucket_for(n) for n in (0, 1, 30, 31, 60, 61, 90, 91)] == [
        "current", "d1_30", "d1_30", "d31_60", "d31_60", "d61_90", "d61_90", "over_90"]


def test_unapplied_credits_are_negative_and_total_includes_them(invoice, customer, admin_user):
    p = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("130.00"), user=admin_user,
                        applications=[(invoice, D("100.00"))])
    create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("10.00"))])
    report = ar_aging(d(10, 20))
    (row,) = report.rows
    assert row.unapplied_credits == D("-40.00") and row.total == D("-40.00")
    assert report.totals.total == D("-40.00") and report.difference == D("0.00")


def test_aging_total_equals_ar_account(invoice, customer, admin_user, ar, service):
    receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("30.00"), user=admin_user,
                    applications=[(invoice, D("30.00"))])
    create_invoice(customer=Customer.objects.create(name="Zed Co"), date=d(10, 9), user=admin_user,
                   lines=[InvoiceLineSpec(service, 2, D("12.50"))])
    report = ar_aging(d(10, 31))
    assert report.totals.total == D("95.00") == report.ledger_balance and report.difference == D("0.00")
    assert [r.customer.name for r in report.rows] == ["Acme Builders", "Zed Co"]


def test_aging_as_of_before_and_after_a_removed_application(invoice, customer, admin_user):
    p = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("100.00"), user=admin_user,
                        applications=[(invoice, D("100.00"))])
    (app,) = p.applications.all()
    remove_application(app, date=d(10, 9), user=admin_user)
    assert ar_aging(d(10, 7)).rows == []
    after = ar_aging(d(10, 10))
    (row,) = after.rows
    assert (row.current, row.unapplied_credits, row.total) == (D("100.00"), D("-100.00"), D("0.00"))
    assert after.difference == D("0.00")


def test_difference_shows_when_ledger_disagrees(invoice, ar, revenue, admin_user):
    from ledger.posting import LineSpec, post_entry

    post_entry(date=d(10, 2), user=admin_user, lines=[LineSpec(ar, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    assert ar_aging(d(10, 31)).difference == D("-5.00")


def test_open_invoices_filters(invoice, customer, admin_user, service):
    other = Customer.objects.create(name="Zed Co")
    create_invoice(customer=other, date=d(10, 20), user=admin_user, terms=Terms.DUE_ON_RECEIPT,
                   lines=[InvoiceLineSpec(service, 1, D("10.00"))])
    rows = open_invoices(d(11, 5))
    assert [(r.invoice.number, r.balance, r.days_overdue) for r in rows] == [("INV-000002", D("10.00"), 16), ("INV-000001", D("100.00"), 5)]
    assert [r.invoice.number for r in open_invoices(d(11, 5), customer=other)] == ["INV-000002"]
    assert [r.invoice.number for r in open_invoices(d(10, 25), overdue_only=True)] == ["INV-000002"]
    assert open_invoices(d(9, 1)) == []


def test_customer_balances(invoice, customer, admin_user, service):
    receive_payment(customer=Customer.objects.create(name="Zed Co"), date=d(10, 5), method=PaymentMethod.CASH,
                    amount=D("25.00"), user=admin_user)
    rows = {r.customer.name: r.balance for r in customer_balances()}
    assert rows == {"Acme Builders": D("100.00"), "Zed Co": D("-25.00")}


def test_open_sales_orders_list_remaining_only(service, customer, admin_user, make_item, inv_settings):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[
        OrderLineSpec(service, 5, D("10.00")),
        OrderLineSpec(service, 2, D("10.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme")])
    first, _ = order.lines.all()
    from sales.posting.invoices import invoice_from_order
    invoice_from_order(order, date=d(10, 5), quantities={first.pk: 5}, user=admin_user)
    rows = open_sales_orders()
    assert [(r.order.number, r.line.fulfillment, r.ordered, r.left) for r in rows] == [("SO-00001", "DROP_SHIP", 2, 2)]


def test_undeposited_funds(customer, admin_user, bank, sales_settings):
    a = receive_payment(customer=customer, date=d(10, 3), method=PaymentMethod.CHECK, amount=D("40.00"), user=admin_user)
    b = receive_payment(customer=customer, date=d(10, 4), method=PaymentMethod.CASH, amount=D("10.00"), user=admin_user)
    make_deposit(payments=[a], bank_account=bank, date=d(10, 6), user=admin_user)
    report = undeposited_funds()
    assert report.payments == [b] and report.total == D("10.00") == report.ledger_balance and report.difference == D("0.00")


def test_drop_ship_awaiting_bill(service, customer, admin_user):
    create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
        InvoiceLineSpec(service, 3, D("10.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme"),
        InvoiceLineSpec(service, 1, D("10.00"))])
    (line,) = drop_ship_awaiting_bill()
    assert (line.invoice.number, line.quantity, line.drop_ship_vendor) == ("INV-000001", 3, "Acme")
    line.drop_ship_billed = True
    line.save()
    assert drop_ship_awaiting_bill() == []


# ---- views ----

@pytest.fixture
def logged(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("name", [
    "ar-aging", "open-invoices", "customer-balances", "open-sales-orders", "undeposited-funds", "drop-ship-awaiting-bill"])
def test_reports_need_login_and_render(client, logged, admin_user, invoice, name):
    client.logout()
    assert client.get(reverse(f"reports:{name}")).status_code == 302
    client.force_login(admin_user)
    r = client.get(reverse(f"reports:{name}"))
    assert r.status_code == 200
    csv_r = client.get(reverse(f"reports:{name}"), {"format": "csv"})
    assert csv_r.status_code == 200 and csv_r["Content-Type"].startswith("text/csv")


def test_ar_aging_csv_content(logged, invoice):
    r = logged.get(reverse("reports:ar-aging"), {"as_of": "2026-11-05", "format": "csv"})
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0] == ["Customer", "Current", "1-30", "31-60", "61-90", "Over 90", "Unapplied credits", "Total"]
    assert rows[1] == ["Acme Builders", "0.00", "100.00", "0.00", "0.00", "0.00", "0.00", "100.00"]
    assert rows[2][0] == "Total" and rows[-1][0] == "Difference" and rows[-1][-1] == "0.00"


def test_bad_date_shows_message(logged):
    r = logged.get(reverse("reports:ar-aging"), {"as_of": "11/05/2026"})
    assert r.status_code == 200 and "Enter a date like 2026-01-31." in r.content.decode()


def test_difference_banner(logged, invoice, ar, revenue, admin_user):
    from ledger.posting import LineSpec, post_entry

    page = logged.get(reverse("reports:ar-aging"), {"as_of": "2026-10-31"}).content.decode()
    assert "banner error" not in page
    post_entry(date=d(10, 2), user=admin_user, lines=[LineSpec(ar, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    page = logged.get(reverse("reports:ar-aging"), {"as_of": "2026-10-31"}).content.decode()
    assert "banner error" in page and "Difference: -5.00" in page

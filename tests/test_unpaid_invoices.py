import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from contacts.models import Customer
from core.report_catalog import catalog
from items.models import ItemType
from sales.checks import _account_balance
from sales.models import PaymentMethod, SalesSettings
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.receipts import CreditLineSpec, create_credit_note, receive_payment
from sales.reports import ar_aging, unpaid_by_customer

D = Decimal


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def _inv(customer, day, amount, user, service):
    return create_invoice(customer=customer, date=day, user=user, lines=[InvoiceLineSpec(service, 1, D(amount))])


# ---- invoices list filter ----

@pytest.fixture
def listed(service, customer, admin_user):
    today = timezone.localdate()
    old = today - timedelta(days=60)
    partly = _inv(customer, old, "100.00", admin_user, service)
    receive_payment(customer=customer, date=old, method=PaymentMethod.CHECK, amount=D("40.00"), user=admin_user,
                    applications=[(partly, D("40.00"))])
    fresh = _inv(customer, today, "50.00", admin_user, service)  # not due yet
    paid = _inv(customer, old, "70.00", admin_user, service)
    receive_payment(customer=customer, date=old, method=PaymentMethod.CHECK, amount=D("70.00"), user=admin_user,
                    applications=[(paid, D("70.00"))])
    voided = _inv(customer, old, "30.00", admin_user, service)
    void_invoice(voided, user=admin_user, reason="mistake")
    return {"partly": partly, "fresh": fresh, "paid": paid, "voided": voided}


def _numbers(response):
    return {r["invoice"].number for r in response.context["rows"]}


def test_unpaid_filter_lists_every_invoice_with_a_balance(client, admin_user, listed):
    client.force_login(admin_user)
    response = client.get(reverse("sales:invoice-list"), {"unpaid": "1"})
    assert _numbers(response) == {listed["partly"].number, listed["fresh"].number}
    assert response.context["unpaid"] is True
    assert "unpaid=1" in response.context["querystring"]
    assert "Unpaid only" in response.content.decode()


def test_overdue_filter_alone_and_with_unpaid(client, admin_user, listed):
    client.force_login(admin_user)
    overdue = _numbers(client.get(reverse("sales:invoice-list"), {"overdue": "1"}))
    assert listed["partly"].number in overdue and listed["fresh"].number not in overdue
    both = _numbers(client.get(reverse("sales:invoice-list"), {"overdue": "1", "unpaid": "1"}))
    assert both == {listed["partly"].number}


def test_unpaid_filter_query_count_is_flat(client, admin_user, customer, service, django_assert_max_num_queries):
    client.force_login(admin_user)
    day = timezone.localdate()
    for _ in range(3):
        _inv(customer, day, "10.00", admin_user, service)
    with django_assert_max_num_queries(20) as small:
        client.get(reverse("sales:invoice-list"), {"unpaid": "1"})
    for _ in range(27):
        _inv(customer, day, "10.00", admin_user, service)
    with django_assert_max_num_queries(len(small.captured_queries)):
        client.get(reverse("sales:invoice-list"), {"unpaid": "1"})


# ---- report ----

@pytest.fixture
def book(service, customer, admin_user):
    zed = Customer.objects.create(name="zed Foods")
    a1 = _inv(customer, date(2026, 8, 1), "100.00", admin_user, service)   # due Aug 31
    a2 = _inv(customer, date(2026, 10, 10), "80.00", admin_user, service)  # due Nov 9
    receive_payment(customer=customer, date=date(2026, 10, 12), method=PaymentMethod.CHECK, amount=D("50.00"),
                    user=admin_user, applications=[(a2, D("30.00"))])  # 20.00 left unapplied
    create_credit_note(customer=customer, date=date(2026, 10, 13), user=admin_user, reason="damaged",
                       lines=[CreditLineSpec("a", D("5.00"))])
    z1 = _inv(zed, date(2026, 6, 1), "200.00", admin_user, service)  # due Jul 1
    return {"a1": a1, "a2": a2, "z1": z1, "zed": zed}


def test_report_groups_buckets_and_subtotals(book, customer):
    report = unpaid_by_customer(date(2026, 10, 20))
    assert [g.customer.name for g in report.groups] == ["Acme Builders", "zed Foods"]
    acme, zed = report.groups
    assert [(a.invoice.number, a.bucket, a.balance) for a in acme.invoices] == [
        (book["a1"].number, "d31_60", D("100.00")), (book["a2"].number, "current", D("50.00"))]
    assert acme.credits == D("-25.00")
    assert acme.totals.d31_60 == D("100.00") and acme.totals.current == D("50.00")
    assert acme.totals.unapplied_credits == D("-25.00") and acme.totals.total == D("125.00")
    (z,) = zed.invoices
    assert z.bucket == "over_90" and z.days_past_due == 111 and zed.credits == D("0.00")
    assert report.totals.total == D("325.00")


@pytest.mark.parametrize("as_of", [date(2026, 10, 20), date(2026, 12, 31)])
def test_grand_total_equals_ar_aging_and_ar_account(book, as_of):
    report = unpaid_by_customer(as_of)
    aging = ar_aging(as_of)
    ledger = _account_balance(SalesSettings.load().ar_account, as_of)
    assert report.totals.total == aging.totals.total == ledger
    assert report.ledger_balance == ledger and report.difference == D("0.00")
    for name in ("current", "d1_30", "d31_60", "d61_90", "over_90", "unapplied_credits"):
        assert getattr(report.totals, name) == getattr(aging.totals, name)


def test_report_page_csv_pdf_and_catalog(client, admin_user, book):
    client.force_login(admin_user)
    url = reverse("reports:unpaid-invoices")
    page = client.get(url, {"as_of": "2026-10-20"}).content.decode()
    assert "Unpaid invoices by customer" in page and "Total Acme Builders" in page
    assert "Unapplied payments and credits" in page and "Total unpaid" in page
    rows = list(csv.reader(io.StringIO(client.get(url, {"as_of": "2026-10-20", "format": "csv"}).content.decode())))
    assert rows[0] == ["Customer", "Invoice", "Date", "Due", "Days past due", "Current", "1-30", "31-60", "61-90",
                       "Over 90", "Credits", "Balance"]
    assert ["Total Acme Builders", "", "", "", "", "50.00", "0.00", "100.00", "0.00", "0.00", "-25.00", "125.00"] in rows
    assert rows[-1][0] == "Total" and rows[-1][-1] == "325.00"
    pdf = client.get(url, {"as_of": "2026-10-20", "format": "pdf"})
    assert pdf["Content-Type"] == "application/pdf"
    slugs = [r["slug"] for g in catalog() for r in g["reports"]]
    assert "unpaid-invoices" in slugs


def test_report_empty_state(client, admin_user, sales_settings):
    client.force_login(admin_user)
    page = client.get(reverse("reports:unpaid-invoices")).content.decode()
    assert "Nothing is owed on this date." in page

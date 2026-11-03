from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer
from items.models import ItemType
from sales.balances import customer_balance
from sales.models import PaymentMethod
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.receipts import (
    CreditLineSpec, apply_credit, create_credit_note, issue_refund, receive_payment, void_payment,
)
from sales.reports import ar_aging
from sales.statements import build_statement

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def script(service, customer, admin_user, cash):
    """Invoice 100 (Sep 1), invoice 250 (Oct 5), partial payment 60 (Oct 10), credit 20 applied (Oct 12),
    unapplied payment 40 (Oct 15), refund 15 of that payment (Oct 20), invoice 80 voided Oct 22."""
    inv1 = create_invoice(customer=customer, date=d(9, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("100.00"))])
    inv2 = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("250.00"))])
    receive_payment(customer=customer, date=d(10, 10), method=PaymentMethod.CHECK, amount=D("60.00"), user=admin_user,
                    applications=[(inv1, D("60.00"))])
    note = create_credit_note(customer=customer, date=d(10, 11), user=admin_user, reason="dented",
                              lines=[CreditLineSpec("dented", D("20.00"))])
    apply_credit(note, inv2, D("20.00"), date=d(10, 12), user=admin_user)
    pay = receive_payment(customer=customer, date=d(10, 15), method=PaymentMethod.CHECK, amount=D("40.00"), user=admin_user)
    issue_refund(source=pay, date=d(10, 20), bank_account=cash, method="CHECK", amount=D("15.00"), user=admin_user)
    inv3 = create_invoice(customer=customer, date=d(10, 21), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("80.00"))])
    void_invoice(inv3, user=admin_user, reason="mistake", date=d(10, 22))
    return customer


def test_aging_can_be_limited_to_one_customer(script, service, admin_user):
    other = Customer.objects.create(name="Zed Co")
    create_invoice(customer=other, date=d(10, 9), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("12.00"))])
    everyone = ar_aging(d(10, 31))
    mine = ar_aging(d(10, 31), customer=script)
    assert [r.customer for r in mine.rows] == [script]
    assert mine.rows[0].total == next(r for r in everyone.rows if r.customer == script).total


def test_rows_signs_and_running_balance(script):
    s = build_statement(script, d(10, 1), d(10, 31))
    assert s.opening == D("100.00")
    kinds = [(r.date, r.kind, r.charge, r.credit, r.balance) for r in s.rows]
    assert kinds == [
        (d(10, 5), "Invoice", D("250.00"), None, D("350.00")),
        (d(10, 10), "Payment", None, D("60.00"), D("290.00")),
        (d(10, 11), "Credit note", None, D("20.00"), D("270.00")),
        (d(10, 15), "Payment", None, D("40.00"), D("230.00")),
        (d(10, 20), "Refund", D("15.00"), None, D("245.00")),
        (d(10, 21), "Invoice", D("80.00"), None, D("325.00")),
        (d(10, 22), "Void of invoice", None, D("80.00"), D("245.00")),
    ]
    assert s.closing == D("245.00")
    assert any(r.is_void for r in s.rows)


def test_closing_matches_customer_balance_and_aging(script):
    for end in (d(9, 30), d(10, 12), d(10, 21), d(10, 22), d(10, 31)):
        s = build_statement(script, d(1, 1), end)
        assert s.closing == customer_balance(script, end)
        aging = ar_aging(end, customer=script).rows
        assert s.aging.total == (aging[0].total if aging else D("0.00")) == s.closing


def test_periods_chain(script):
    first = build_statement(script, d(9, 1), d(10, 14))
    second = build_statement(script, d(10, 15), d(10, 31))
    assert second.opening == first.closing


def test_payment_voided_later_counts_until_its_void_date(service, customer, admin_user):
    create_invoice(customer=customer, date=d(10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("100.00"))])
    pay = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("30.00"), user=admin_user)
    void_payment(pay, user=admin_user, reason="bounced", date=d(10, 25))
    before = build_statement(customer, d(10, 1), d(10, 20))
    after = build_statement(customer, d(10, 1), d(10, 31))
    assert before.closing == D("70.00") == customer_balance(customer, d(10, 20))
    assert after.closing == D("100.00") == customer_balance(customer, d(10, 31))
    assert [r.kind for r in after.rows][-1] == "Void of payment"


def test_empty_customer(customer):
    s = build_statement(customer, d(10, 1), d(10, 31))
    assert (s.opening, s.closing, s.rows) == (D("0.00"), D("0.00"), [])
    assert s.aging.total == D("0.00") and s.open_invoices == []


def test_all_statements_sum_to_the_ar_account(script, service, admin_user):
    other = Customer.objects.create(name="Zed Co")
    create_invoice(customer=other, date=d(10, 9), user=admin_user, lines=[InvoiceLineSpec(service, 2, D("12.50"))])
    total = sum(build_statement(c, d(1, 1), d(10, 31)).closing for c in Customer.objects.all())
    assert total == ar_aging(d(10, 31)).ledger_balance


def test_open_invoices_listed_as_of_end(script):
    s = build_statement(script, d(10, 1), d(10, 31))
    assert [(o.invoice.number, o.balance) for o in s.open_invoices] == [
        (script.invoices.get(date=d(9, 1)).number, D("40.00")), (script.invoices.get(date=d(10, 5)).number, D("230.00"))]


# ---- views and PDF ----
from django.urls import reverse  # noqa: E402

from sales.pdf import render_statement_html, statement_pdf  # noqa: E402


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


def test_login_required(client, customer):
    assert client.get(reverse("sales:statement", args=[customer.pk])).status_code == 302
    assert client.get(reverse("sales:statement-pdf", args=[customer.pk])).status_code == 302


def test_unknown_customer_is_404(adm):
    assert adm.get("/sales/customers/9999/statement/").status_code == 404
    assert adm.get("/sales/customers/9999/statement/pdf/").status_code == 404


def test_page_shows_the_figures(adm, script):
    r = adm.get(reverse("sales:statement", args=[script.pk]), {"start": "2026-10-01", "end": "2026-10-31"})
    assert r.status_code == 200
    html = r.content.decode()
    assert "Balance forward" in html and "100.00" in html and "245.00" in html and "Download PDF" in html
    assert "Void of invoice" in html


def test_page_defaults_to_the_last_90_days(adm, customer):
    from datetime import timedelta

    from django.utils import timezone

    r = adm.get(reverse("sales:statement", args=[customer.pk]))
    today = timezone.localdate()
    assert r.status_code == 200 and (today - timedelta(days=90)).isoformat() in r.content.decode()


def test_start_after_end_is_an_error(adm, customer):
    r = adm.get(reverse("sales:statement", args=[customer.pk]), {"start": "2026-10-31", "end": "2026-10-01"})
    assert r.status_code == 200 and "The start date must be on or before the end date." in r.content.decode()
    assert "Download PDF" not in r.content.decode()


def test_links_on_customer_pages(adm, customer):
    url = reverse("sales:statement", args=[customer.pk])
    assert url in adm.get(reverse("contacts:list")).content.decode()
    assert url in adm.get(reverse("contacts:detail", args=[customer.pk])).content.decode()


def test_pdf_html_and_bytes(script, sales_settings):
    sales_settings.company_name = "DNB Supply Co."
    sales_settings.save()
    s = build_statement(script, d(10, 1), d(10, 31))
    html = render_statement_html(s)
    for text in ("STATEMENT", "DNB Supply Co.", "Acme Builders", "Balance forward", "Aging as of", "Balance due"):
        assert text in html
    assert statement_pdf(s).startswith(b"%PDF")


def test_pdf_view_is_inline(adm, script):
    r = adm.get(reverse("sales:statement-pdf", args=[script.pk]), {"start": "2026-10-01", "end": "2026-10-31"})
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    assert r["Content-Disposition"] == 'inline; filename="Statement-acme-builders-2026-10-31.pdf"'
    assert r.content.startswith(b"%PDF")


def test_pdf_view_bad_dates_is_400(adm, customer):
    r = adm.get(reverse("sales:statement-pdf", args=[customer.pk]), {"start": "2026-10-31", "end": "2026-10-01"})
    assert r.status_code == 400


def test_long_statement_renders(service, customer, admin_user):
    for i in range(80):
        create_invoice(customer=customer, date=d(10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("1.00"))])
    s = build_statement(customer, d(10, 1), d(10, 31))
    assert len(s.rows) == 80 and statement_pdf(s).startswith(b"%PDF")


def test_payments_and_credits_list_the_invoices_they_paid(script):
    s = build_statement(script, d(9, 1), d(10, 31))
    pay60 = next(r for r in s.rows if r.kind == "Payment" and r.credit == D("60.00"))
    assert [(a.number, a.amount) for a in pay60.applied] == [(script.invoices.get(date=d(9, 1)).number, D("60.00"))]
    assert pay60.unapplied == D("0.00")
    pay40 = next(r for r in s.rows if r.kind == "Payment" and r.credit == D("40.00"))
    assert pay40.applied == [] and pay40.unapplied == D("25.00")  # 15.00 of it was refunded
    note = next(r for r in s.rows if r.kind == "Credit note")
    assert [(a.number, a.amount) for a in note.applied] == [(script.invoices.get(date=d(10, 5)).number, D("20.00"))]


def test_applications_are_as_of_the_statement_end(script):
    s = build_statement(script, d(9, 1), d(10, 10))  # the credit is applied Oct 12, after this end
    assert next(r for r in s.rows if r.kind == "Payment").applied
    s2 = build_statement(script, d(9, 1), d(10, 31))
    html = render_statement_html(s2)
    assert "Check" in html and "Payments and credits not yet applied" in html


# ---- invoice-first layout: each invoice, with the payments and credits applied to it below ----
def test_each_invoice_lists_its_payments_split_by_amount(service, customer, admin_user):
    inv1 = create_invoice(customer=customer, date=d(10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("100.00"))])
    inv2 = create_invoice(customer=customer, date=d(10, 2), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("50.00"))])
    receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("50.00"), reference="123",
                    user=admin_user, applications=[(inv1, D("50.00"))])
    receive_payment(customer=customer, date=d(10, 6), method=PaymentMethod.CHECK, amount=D("100.00"), reference="124",
                    user=admin_user, applications=[(inv1, D("50.00")), (inv2, D("50.00"))])
    s = build_statement(customer, d(10, 1), d(10, 31))
    b1, b2 = s.invoices
    assert (b1.number, b1.total, b1.balance) == (inv1.number, D("100.00"), D("0.00"))
    assert [(a.number, a.detail, a.amount) for a in b1.applied] == [
        ("PMT-00001", "Check 123", D("50.00")), ("PMT-00002", "Check 124", D("50.00"))]
    assert [(a.number, a.detail, a.amount) for a in b2.applied] == [("PMT-00002", "Check 124", D("50.00"))]
    assert b2.balance == D("0.00") and s.unapplied == []


def test_invoice_blocks_reconcile_to_the_closing_balance(script):
    s = build_statement(script, d(10, 1), d(10, 31))
    owed = sum((b.balance for b in s.invoices), D("0.00"))
    credits = sum((u.amount for u in s.unapplied), D("0.00"))
    assert owed - credits == s.closing
    # the 40.00 payment has 15.00 refunded, 25.00 left; the credit note is fully applied
    assert [(u.number, u.amount) for u in s.unapplied] == [("PMT-00002", D("25.00"))]
    assert [(b.number, b.balance) for b in s.invoices] == [
        (script.invoices.get(date=d(9, 1)).number, D("40.00")), (script.invoices.get(date=d(10, 5)).number, D("230.00"))]


def test_voided_invoice_and_old_settled_invoice_are_left_out(script):
    s = build_statement(script, d(10, 1), d(10, 31))
    assert script.invoices.get(date=d(10, 21)).number not in [b.number for b in s.invoices]
    later = build_statement(script, d(10, 11), d(10, 31))  # inv1 (Sep) is still open, so it stays
    assert len(later.invoices) == 2

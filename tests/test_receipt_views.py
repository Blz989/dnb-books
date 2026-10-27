import html
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from items.models import ItemType
from sales.balances import credit_unapplied, invoice_balance, payment_unapplied
from sales.models import (
    CreditNote, Deposit, DocStatus, Payment, PaymentMethod, Refund, RefundMethod,
)
from sales.posting.deposits import make_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import (
    CreditLineSpec, create_credit_note, issue_refund, receive_payment,
)

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
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def two_invoices(service, customer, admin_user):
    a = create_invoice(customer=customer, date=date(2026, 10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("60.00"))])
    b = create_invoice(customer=customer, date=date(2026, 10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("50.00"))])
    return a, b


def text(response):
    return html.unescape(response.content.decode())


def test_pages_need_login(client):
    for url in ("/sales/payments/", "/sales/payments/new/", "/sales/deposits/", "/sales/deposits/new/", "/sales/credits/",
                "/sales/credits/new/", "/sales/refunds/new/?payment=PMT-00001"):
        assert client.get(url).status_code == 302


def test_receive_payment_shows_open_invoices_after_customer_is_chosen(adm, customer, two_invoices):
    page = text(adm.get("/sales/payments/new/"))
    assert "Choose a customer to see what they owe." in page and "INV-000001" not in page
    page = text(adm.get(f"/sales/payments/new/?customer={customer.pk}"))
    assert "INV-000001" in page and "INV-000002" in page and 'name="apply-' in page and "Apply oldest first" in page
    assert page.index("INV-000001") < page.index("INV-000002")


def test_payment_applied_across_two_invoices(adm, customer, two_invoices):
    a, b = two_invoices
    r = adm.post("/sales/payments/new/", {
        "token": "p1", "customer": customer.pk, "date": "2026-10-10", "method": "CHECK", "reference": "4410", "amount": "110.00",
        f"apply-{a.pk}": "60.00", f"apply-{b.pk}": "50.00"})
    assert r.status_code == 302 and r["Location"] == "/sales/payments/PMT-00001/"
    assert invoice_balance(a) == invoice_balance(b) == D("0.00")
    page = text(adm.get("/sales/payments/PMT-00001/"))
    assert "INV-000001" in page and "INV-000002" in page and "4410" in page and "Not deposited yet" in page


def test_apply_oldest_first_button_without_javascript(adm, customer, two_invoices):
    r = adm.post("/sales/payments/new/", {"token": "p2", "customer": customer.pk, "date": "2026-10-10", "method": "CHECK",
                                          "amount": "75.00", "fill": "oldest"})
    page = text(r)
    assert r.status_code == 200 and 'value="60.00"' in page and 'value="15.00"' in page
    assert not Payment.objects.exists()


def test_overpayment_shows_as_credit_on_the_customer_page(adm, customer, two_invoices):
    a, b = two_invoices
    adm.post("/sales/payments/new/", {
        "token": "p3", "customer": customer.pk, "date": "2026-10-10", "method": "CASH", "amount": "130.00",
        f"apply-{a.pk}": "60.00", f"apply-{b.pk}": "50.00"})
    page = text(adm.get(f"/customers/{customer.pk}/"))
    assert "PMT-00001" in page and "20.00" in page and "/sales/payments/PMT-00001/apply/" in page


def test_payment_errors(adm, customer, two_invoices, bank):
    a, _ = two_invoices
    base = {"token": "e1", "customer": customer.pk, "date": "2026-10-10", "method": "CHECK", "amount": "100.00"}
    page = text(adm.post("/sales/payments/new/", {**base, f"apply-{a.pk}": "75.00"}))
    assert "INV-000001 has 60.00 left to pay." in page
    page = text(adm.post("/sales/payments/new/", {**base, "token": "e2", "amount": "0"}))
    assert "Enter an amount above zero." in page
    page = text(adm.post("/sales/payments/new/", {**base, "token": "e3", "method": "ACH"}))
    assert "Choose the bank account the ACH went to." in page
    page = text(adm.post("/sales/payments/new/", {**base, "token": "e4", f"apply-{a.pk}": "abc"}))
    assert "INV-000001: enter an amount like 25.00." in page
    assert not Payment.objects.exists()


def test_ach_payment_goes_to_bank(adm, customer, bank, two_invoices):
    r = adm.post("/sales/payments/new/", {"token": "a1", "customer": customer.pk, "date": "2026-10-10", "method": "ACH",
                                          "amount": "20.00", "bank_account": bank.pk})
    assert r.status_code == 302 and Payment.objects.get().deposited_to == bank


def test_payment_double_submit_saves_once(adm, customer, two_invoices):
    data = {"token": "dup", "customer": customer.pk, "date": "2026-10-10", "method": "CHECK", "amount": "10.00"}
    adm.post("/sales/payments/new/", data)
    r = adm.post("/sales/payments/new/", data, follow=True)
    assert Payment.objects.count() == 1 and "This payment was already saved." in text(r)


def test_invoice_link_prefills_amount_and_application(adm, customer, two_invoices):
    page = text(adm.get(f"/sales/payments/new/?customer={customer.pk}&invoice=INV-000002"))
    assert 'value="50.00"' in page


def test_apply_a_payments_credit_later(adm, customer, two_invoices):
    a, b = two_invoices
    p = receive_payment(customer=customer, date=date(2026, 10, 10), method=PaymentMethod.CHECK, amount=D("30.00"), user=a.created_by)
    page = text(adm.get("/sales/payments/PMT-00001/apply/"))
    assert "30.00 left to apply" in page and 'value="30.00"' in page
    r = adm.post("/sales/payments/PMT-00001/apply/", {"token": "ap1", "date": "2026-10-11", f"apply-{b.pk}": "30.00"})
    assert r.status_code == 302
    assert invoice_balance(b) == D("20.00") and payment_unapplied(p) == D("0.00")
    page = text(adm.post("/sales/payments/PMT-00001/apply/", {"token": "ap2", "date": "2026-10-11"}))
    assert "Enter an amount for at least one invoice." in page


def test_apply_over_the_payment_shows_message(adm, customer, two_invoices, admin_user):
    a, _ = two_invoices
    receive_payment(customer=customer, date=date(2026, 10, 10), method=PaymentMethod.CHECK, amount=D("30.00"), user=admin_user)
    page = text(adm.post("/sales/payments/PMT-00001/apply/", {"token": "ap3", "date": "2026-10-11", f"apply-{a.pk}": "40.00"}))
    assert "This payment has 30.00 left to apply." in page


def test_deposit_removes_payments_from_the_undeposited_list(adm, customer, bank, admin_user, sales_settings):
    p1 = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("100.00"), user=admin_user)
    p2 = receive_payment(customer=customer, date=date(2026, 10, 4), method=PaymentMethod.CASH, amount=D("40.50"), user=admin_user)
    page = text(adm.get("/sales/deposits/new/"))
    assert "PMT-00001" in page and "PMT-00002" in page and 'id="deposit-total"' in page
    r = adm.post("/sales/deposits/new/", {"token": "d1", "date": "2026-10-06", "bank_account": bank.pk, "payments": [p1.pk, p2.pk]})
    assert r.status_code == 302 and r["Location"] == "/sales/deposits/DEP-00001/"
    assert "Nothing is waiting to be deposited." in text(adm.get("/sales/deposits/new/"))
    detail = text(adm.get("/sales/deposits/DEP-00001/"))
    assert "140.50" in detail and "PMT-00001" in detail
    assert "DEP-00001" in text(adm.get("/sales/deposits/"))


def test_deposit_needs_a_choice_and_a_bank(adm, customer, bank, admin_user, sales_settings):
    p = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("10.00"), user=admin_user)
    assert "Choose at least one payment." in text(adm.post("/sales/deposits/new/", {"token": "d2", "date": "2026-10-06", "bank_account": bank.pk}))
    assert "Choose the bank account for the deposit." in text(adm.post("/sales/deposits/new/", {"token": "d3", "date": "2026-10-06", "payments": [p.pk]}))
    assert "The deposit date can't be before PMT-00001's date." in text(
        adm.post("/sales/deposits/new/", {"token": "d4", "date": "2026-10-01", "bank_account": bank.pk, "payments": [p.pk]}))
    assert not Deposit.objects.exists()


def test_deposit_void_admin_only(adm, std, customer, bank, admin_user, sales_settings):
    p = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("10.00"), user=admin_user)
    make_deposit(payments=[p], bank_account=bank, date=date(2026, 10, 4), user=admin_user)
    assert std.get("/sales/deposits/DEP-00001/void/").status_code == 403
    assert "Void" in text(adm.get("/sales/deposits/DEP-00001/"))
    r = adm.post("/sales/deposits/DEP-00001/void/", {"date": "2026-10-04", "reason": "Wrong bank"})
    assert r.status_code == 302 and Deposit.objects.get().status == DocStatus.VOID
    assert "PMT-00001" in text(adm.get("/sales/deposits/new/"))


def test_payment_void_in_deposit_shows_message(adm, customer, bank, admin_user, sales_settings):
    p = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("10.00"), user=admin_user)
    make_deposit(payments=[p], bank_account=bank, date=date(2026, 10, 4), user=admin_user)
    r = adm.post("/sales/payments/PMT-00001/void/", {"date": "2026-10-04", "reason": "Bounced"})
    assert "PMT-00001 is in deposit DEP-00001. Void the deposit first." in text(r)


def test_credit_note_form_and_detail(adm, customer, two_invoices, sales_settings):
    r = adm.post("/sales/credits/new/", {
        "token": "c1", "customer": customer.pk, "date": "2026-10-06", "reason": "Damaged",
        "lines-0-description": "Damaged goods", "lines-0-amount": "35.00", "lines-0-account": "",
        "lines-1-description": "Restocking", "lines-1-amount": "5.00", "lines-1-account": ""})
    assert r.status_code == 302 and r["Location"] == "/sales/credits/CN-00001/"
    note = CreditNote.objects.get()
    assert credit_unapplied(note) == D("40.00")
    page = text(adm.get("/sales/credits/CN-00001/"))
    assert "Damaged goods" in page and "40.00" in page
    a, b = two_invoices
    r = adm.post("/sales/credits/CN-00001/apply/", {"token": "c2", "date": "2026-10-07", f"apply-{a.pk}": "40.00"})
    assert r.status_code == 302 and invoice_balance(a) == D("20.00") and credit_unapplied(note) == D("0.00")


def test_credit_note_errors(adm, customer, sales_settings):
    page = text(adm.post("/sales/credits/new/", {"token": "c3", "customer": customer.pk, "date": "2026-10-06"}))
    assert "Add at least one line." in page
    page = text(adm.post("/sales/credits/new/", {"token": "c4", "customer": customer.pk, "date": "2026-10-06",
                                                 "lines-0-description": "x", "lines-0-amount": "zero"}))
    assert "Line 1: enter an amount above zero." in page


def test_refund_form_and_amount_message(adm, customer, bank, admin_user, sales_settings):
    p = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("100.00"), user=admin_user)
    page = text(adm.get("/sales/refunds/new/?payment=PMT-00001"))
    assert "100.00 available to refund" in page and 'value="100.00"' in page
    base = {"token": "r1", "source_payment": "PMT-00001", "date": "2026-10-08", "method": "CHECK", "reference": "2001",
            "bank_account": bank.pk}
    page = text(adm.post("/sales/refunds/new/", {**base, "amount": "150.00"}))
    assert "Only 100.00 is available to refund from PMT-00001." in page
    r = adm.post("/sales/refunds/new/", {**base, "token": "r2", "amount": "40.00"})
    assert r.status_code == 302 and r["Location"] == "/sales/refunds/REF-00001/"
    assert payment_unapplied(p) == D("60.00")
    assert "40.00" in text(adm.get("/sales/refunds/REF-00001/"))
    assert "REF-00001" in text(adm.get("/sales/payments/PMT-00001/"))


def test_refund_from_credit_note(adm, customer, bank, admin_user, sales_settings):
    create_credit_note(customer=customer, date=date(2026, 10, 3), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("25.00"))])
    r = adm.post("/sales/refunds/new/", {"token": "r3", "source_credit": "CN-00001", "date": "2026-10-08", "method": "ACH",
                                         "bank_account": bank.pk, "amount": "25.00"})
    assert r.status_code == 302 and Refund.objects.get().source_credit_note.number == "CN-00001"


def test_standard_users_get_403_on_refunds_and_voids(std, customer, bank, admin_user, sales_settings):
    p = receive_payment(customer=customer, date=date(2026, 10, 3), method=PaymentMethod.CHECK, amount=D("100.00"), user=admin_user)
    note = create_credit_note(customer=customer, date=date(2026, 10, 3), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("25.00"))])
    r = issue_refund(source=p, date=date(2026, 10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("5.00"), user=admin_user)
    assert std.get("/sales/refunds/new/?payment=PMT-00001").status_code == 403
    assert std.post("/sales/refunds/new/", {"source_payment": "PMT-00001"}).status_code == 403
    for url in ("/sales/payments/PMT-00001/void/", "/sales/credits/CN-00001/void/", f"/sales/refunds/{r.number}/void/"):
        assert std.get(url).status_code == 403, url
        assert std.post(url, {"date": "2026-10-08", "reason": "x"}).status_code == 403, url


def test_standard_user_can_receive_and_apply(std, customer, two_invoices):
    a, _ = two_invoices
    r = std.post("/sales/payments/new/", {"token": "s1", "customer": customer.pk, "date": "2026-10-10", "method": "CHECK",
                                          "amount": "60.00", f"apply-{a.pk}": "60.00"})
    assert r.status_code == 302 and invoice_balance(a) == D("0.00")


def test_void_a_payment_reopens_its_invoices(adm, customer, two_invoices, admin_user):
    a, _ = two_invoices
    receive_payment(customer=customer, date=date(2026, 10, 10), method=PaymentMethod.CHECK, amount=D("60.00"), user=admin_user,
                    applications=[(a, D("60.00"))])
    r = adm.post("/sales/payments/PMT-00001/void/", {"date": "2026-10-10", "reason": "Bounced"})
    assert r.status_code == 302 and invoice_balance(a) == D("60.00")
    assert "Voided on 2026-10-10: Bounced" in text(adm.get("/sales/payments/PMT-00001/"))


def test_void_credit_and_refund_screens(adm, customer, bank, admin_user, sales_settings):
    note = create_credit_note(customer=customer, date=date(2026, 10, 3), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("25.00"))])
    ref = issue_refund(source=note, date=date(2026, 10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("5.00"), user=admin_user)
    r = adm.post("/sales/credits/CN-00001/void/", {"date": "2026-10-08", "reason": "x"})
    assert "CN-00001 has a refund drawn from it. Void the refund first." in text(r)
    assert adm.post(f"/sales/refunds/{ref.number}/void/", {"date": "2026-10-08", "reason": "x"}).status_code == 302
    assert adm.post("/sales/credits/CN-00001/void/", {"date": "2026-10-08", "reason": "Entered twice"}).status_code == 302


def test_lists_render(adm, customer, two_invoices, admin_user, sales_settings):
    receive_payment(customer=customer, date=date(2026, 10, 10), method=PaymentMethod.CHECK, amount=D("10.00"), user=admin_user)
    create_credit_note(customer=customer, date=date(2026, 10, 3), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("25.00"))])
    assert "PMT-00001" in text(adm.get("/sales/payments/?method=CHECK"))
    assert "PMT-00001" not in text(adm.get("/sales/payments/?method=ACH"))
    assert "CN-00001" in text(adm.get("/sales/credits/"))


def test_unknown_numbers_are_404(adm):
    for url in ("/sales/payments/PMT-99999/", "/sales/payments/PMT-99999/apply/", "/sales/credits/CN-99999/",
                "/sales/deposits/DEP-99999/", "/sales/refunds/REF-99999/", "/sales/refunds/new/"):
        assert adm.get(url).status_code == 404, url

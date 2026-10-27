from datetime import date
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from sales.balances import (
    credit_unapplied, customer_balance, invoice_balance, invoice_total, payment_unapplied,
)
from sales.errors import SalesSettingsMissing
from sales.models import (
    CreditApplication, CreditNote, CreditNoteLine, DocStatus, DocumentSequence, Invoice, InvoiceLine, Payment,
    PaymentApplication, PaymentMethod, Refund, RefundMethod, SalesSettings,
)

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def invoice(customer, admin_user, make_item):
    item = make_item("BOLT-1")
    inv = Invoice.objects.create(
        number="INV-000001", customer=customer, date=d(10, 1), due_date=d(10, 31), created_by=admin_user)
    InvoiceLine.objects.create(invoice=inv, item=item, quantity=10, price=D("10.0000"), amount=D("100.00"))
    return inv


@pytest.fixture
def payment(customer, admin_user, sales_settings):
    return Payment.objects.create(
        number="PMT-00001", customer=customer, date=d(10, 5), method=PaymentMethod.CHECK, amount=D("100.00"),
        deposited_to=sales_settings.undeposited_account, created_by=admin_user)


def test_sequences_are_seeded_by_migration(db):
    got = {s.doc_type: (s.prefix, s.padding, s.next_number) for s in DocumentSequence.objects.all()}
    assert got == {
        "SO": ("SO-", 5, 1), "INV": ("INV-", 6, 1), "PMT": ("PMT-", 5, 1),
        "CN": ("CN-", 5, 1), "DEP": ("DEP-", 5, 1), "REF": ("REF-", 5, 1),
    }


def test_settings_configured_only_when_all_accounts_set(db, sales_settings):
    assert SalesSettings.load().is_configured
    empty = SalesSettings.load()
    empty.returns_account = None
    empty.save()
    assert not SalesSettings.load().is_configured
    with pytest.raises(SalesSettingsMissing, match="Set up sales accounts first."):
        SalesSettings.load().require_configured()


def test_invoice_total_and_balance(invoice):
    assert invoice_total(invoice) == D("100.00")
    assert invoice_balance(invoice) == D("100.00")


def test_balances_as_of_respect_removed_applications(invoice, payment):
    app = PaymentApplication.objects.create(payment=payment, invoice=invoice, amount=D("100.00"), applied_on=d(10, 5))
    assert invoice_balance(invoice) == D("0.00")
    assert payment_unapplied(payment) == D("0.00")
    app.removed, app.removed_on = True, d(10, 9)
    app.save()
    assert invoice_balance(invoice) == D("100.00")
    assert invoice_balance(invoice, d(10, 4)) == D("100.00")
    assert invoice_balance(invoice, d(10, 6)) == D("0.00")
    assert invoice_balance(invoice, d(10, 10)) == D("100.00")
    assert payment_unapplied(payment, d(10, 6)) == D("0.00")
    assert payment_unapplied(payment, d(10, 10)) == D("100.00")


def test_removal_on_the_as_of_date_already_counts_as_removed(invoice, payment):
    PaymentApplication.objects.create(
        payment=payment, invoice=invoice, amount=D("40.00"), applied_on=d(10, 5), removed=True, removed_on=d(10, 9))
    assert invoice_balance(invoice, d(10, 8)) == D("60.00")
    assert invoice_balance(invoice, d(10, 9)) == D("100.00")


def test_document_not_counted_before_its_date_or_after_void(invoice, payment):
    assert invoice_balance(invoice, d(9, 30)) == D("0.00")
    assert payment_unapplied(payment, d(10, 4)) == D("0.00")
    invoice.status, invoice.voided_on = DocStatus.VOID, d(10, 20)
    invoice.save()
    assert invoice_balance(invoice, d(10, 19)) == D("100.00")
    assert invoice_balance(invoice, d(10, 20)) == D("0.00")
    assert invoice_balance(invoice) == D("0.00")


def test_unapplied_payment_is_reduced_by_refunds(customer, payment, admin_user, bank):
    Refund.objects.create(
        number="REF-00001", customer=customer, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH,
        amount=D("30.00"), source_payment=payment, created_by=admin_user)
    assert payment_unapplied(payment) == D("70.00")
    assert payment_unapplied(payment, d(10, 7)) == D("100.00")


def test_credit_note_unapplied_and_customer_balance(customer, invoice, admin_user, sales_settings):
    note = CreditNote.objects.create(number="CN-00001", customer=customer, date=d(10, 6), created_by=admin_user)
    CreditNoteLine.objects.create(credit_note=note, account=sales_settings.returns_account, amount=D("25.00"))
    assert credit_unapplied(note) == D("25.00")
    assert customer_balance(customer) == D("75.00")
    CreditApplication.objects.create(credit_note=note, invoice=invoice, amount=D("25.00"), applied_on=d(10, 7))
    assert credit_unapplied(note) == D("0.00")
    assert invoice_balance(invoice) == D("75.00")
    assert customer_balance(customer) == D("75.00")
    assert customer_balance(customer, d(10, 6)) == D("75.00")


def test_overpayment_is_customer_credit(customer, invoice, payment):
    PaymentApplication.objects.create(payment=payment, invoice=invoice, amount=D("60.00"), applied_on=d(10, 5))
    assert invoice_balance(invoice) == D("40.00")
    assert payment_unapplied(payment) == D("40.00")
    assert customer_balance(customer) == D("0.00")


def test_refund_needs_exactly_one_source(customer, admin_user, bank, payment):
    base = dict(
        number="REF-00001", customer=customer, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH,
        amount=D("5.00"), created_by=admin_user)
    with pytest.raises(IntegrityError), transaction.atomic():
        Refund.objects.create(**base)
    note = CreditNote.objects.create(number="CN-00001", customer=customer, date=d(10, 6), created_by=admin_user)
    with pytest.raises(IntegrityError), transaction.atomic():
        Refund.objects.create(**base, source_payment=payment, source_credit_note=note)
    Refund.objects.create(**base, source_payment=payment)

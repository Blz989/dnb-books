"""Payments, credit notes, refunds, and the applications that tie them to invoices."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from typing import Literal

from django.db import transaction

from contacts.models import Customer
from core.roles import require_admin
from ledger.models import Account, AccountSubtype
from ledger.periods import assert_open
from ledger.posting import LineSpec, post_entry, void_entry

from ..balances import credit_unapplied, invoice_balance, payment_unapplied
from ..errors import ReceiptError
from ..models import (
    CreditApplication, CreditNote, CreditNoteLine, DocStatus, DocType, Invoice, Payment, PaymentApplication,
    PaymentMethod, Refund, RefundMethod, SalesSettings,
)
from ..numbering import next_number

ZERO = Decimal("0.00")
BANK_METHODS = (PaymentMethod.ACH, PaymentMethod.WIRE)


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


def _check_amount(amount: Decimal, message: str = "Enter an amount above zero.") -> None:
    if not isinstance(amount, Decimal):
        raise TypeError("Amounts must be Decimal, never float or int.")
    if amount <= 0:
        raise ReceiptError(message)
    if amount.as_tuple().exponent < -2:
        raise ReceiptError("Amounts can have at most 2 decimal places.")


def _check_not_before(date: date_type, *docs) -> None:
    for doc in docs:
        if date < doc.date:
            raise ReceiptError(f"The date can't be before {doc.number}'s date.")


def _check_invoice(invoice: Invoice, customer: Customer) -> Invoice:
    invoice = Invoice.objects.get(pk=invoice.pk)
    if invoice.customer_id != customer.pk:
        raise ReceiptError(f"{invoice.number} belongs to a different customer.")
    if invoice.status == DocStatus.VOID:
        raise ReceiptError(f"{invoice.number} is void.")
    return invoice


def _check_open(doc) -> None:
    if doc.status == DocStatus.VOID:
        raise ReceiptError(f"{doc.number} is void.")


# ---- payments ----

def _add_payment_application(payment: Payment, invoice: Invoice, amount: Decimal, date: date_type) -> PaymentApplication:
    _check_amount(amount)
    invoice = _check_invoice(invoice, payment.customer)
    _check_not_before(date, payment, invoice)
    unapplied = payment_unapplied(payment)
    if amount > unapplied:
        raise ReceiptError(f"This payment has {_money(unapplied)} left to apply.")
    balance = invoice_balance(invoice)
    if amount > balance:
        raise ReceiptError(f"{invoice.number} has {_money(balance)} left to pay.")
    return PaymentApplication.objects.create(payment=payment, invoice=invoice, amount=amount, applied_on=date)


def receive_payment(
    *, customer: Customer, date: date_type, method: PaymentMethod, amount: Decimal, user, reference: str = "",
    bank_account: Account | None = None, applications: Sequence[tuple[Invoice, Decimal]] = (),
) -> Payment:
    with transaction.atomic():
        settings = SalesSettings.load().require_configured()
        _check_amount(amount)
        if method in BANK_METHODS:
            if bank_account is None or bank_account.subtype != AccountSubtype.BANK:
                raise ReceiptError(f"Choose the bank account the {PaymentMethod(method).label} went to.")
            deposited_to = bank_account
        else:
            deposited_to = settings.undeposited_account
        number = next_number(DocType.PMT)
        entry = post_entry(
            date=date, user=user, memo=f"Payment {number} {customer.name}", reference=reference,
            source_type="sales.payment", source_id=number,
            lines=[LineSpec(deposited_to, debit=amount), LineSpec(settings.ar_account, credit=amount)],
        )
        payment = Payment.objects.create(
            number=number, customer=customer, date=date, method=method, reference=reference, amount=amount,
            deposited_to=deposited_to, journal_entry=entry, created_by=user,
        )
        for invoice, applied in applications:
            _add_payment_application(payment, invoice, applied, date)
        return payment


def apply_payment(payment: Payment, invoice: Invoice, amount: Decimal, *, date: date_type, user) -> PaymentApplication:
    with transaction.atomic():
        current = Payment.objects.select_related("customer").get(pk=payment.pk)
        _check_open(current)
        return _add_payment_application(current, invoice, amount, date)


def oldest_first(customer: Customer, amount: Decimal) -> list[tuple[Invoice, Decimal]]:
    """Spread an amount over the customer's open invoices, oldest due date first."""
    plan, left = [], amount
    invoices = customer.invoices.filter(status=DocStatus.OPEN).order_by("due_date", "date", "id")
    for invoice in invoices:
        if left <= 0:
            break
        balance = invoice_balance(invoice)
        if balance <= 0:
            continue
        take = min(balance, left)
        plan.append((invoice, take))
        left -= take
    return plan


def remove_application(app: PaymentApplication | CreditApplication, *, date: date_type, user) -> None:
    with transaction.atomic():
        current = type(app).objects.select_related("invoice").get(pk=app.pk)
        if current.removed:
            raise ReceiptError("That application was already removed.")
        if date < current.applied_on:
            raise ReceiptError("The date can't be before the application date.")
        current.removed = True
        current.removed_on = date
        current.save()


# ---- credit notes ----

@dataclass(frozen=True)
class CreditLineSpec:
    description: str
    amount: Decimal
    account: Account | None = None


def create_credit_note(
    *, customer: Customer, date: date_type, lines: Sequence[CreditLineSpec], reason: str, user,
) -> CreditNote:
    with transaction.atomic():
        settings = SalesSettings.load().require_configured()
        if not lines:
            raise ReceiptError("Add at least one line.")
        resolved = []
        for n, line in enumerate(lines, 1):
            _check_amount(line.amount, f"Line {n}: enter an amount above zero.")
            account = line.account or settings.returns_account
            if account.subtype == AccountSubtype.ACCOUNTS_RECEIVABLE:
                raise ReceiptError(f"Line {n}: choose an account other than accounts receivable.")
            resolved.append((line, account))
        total = sum((l.amount for l, _ in resolved), ZERO)
        number = next_number(DocType.CN)
        entry = post_entry(
            date=date, user=user, memo=f"Credit note {number} {customer.name}",
            source_type="sales.credit_note", source_id=number,
            lines=[LineSpec(account, debit=l.amount, description=l.description) for l, account in resolved]
            + [LineSpec(settings.ar_account, credit=total)],
        )
        note = CreditNote.objects.create(
            number=number, customer=customer, date=date, reason=reason, journal_entry=entry, created_by=user)
        for line, account in resolved:
            CreditNoteLine.objects.create(credit_note=note, description=line.description, account=account, amount=line.amount)
        return note


def apply_credit(credit_note: CreditNote, invoice: Invoice, amount: Decimal, *, date: date_type, user) -> CreditApplication:
    with transaction.atomic():
        current = CreditNote.objects.select_related("customer").get(pk=credit_note.pk)
        _check_open(current)
        _check_amount(amount)
        invoice = _check_invoice(invoice, current.customer)
        _check_not_before(date, current, invoice)
        unapplied = credit_unapplied(current)
        if amount > unapplied:
            raise ReceiptError(f"This credit note has {_money(unapplied)} left to apply.")
        balance = invoice_balance(invoice)
        if amount > balance:
            raise ReceiptError(f"{invoice.number} has {_money(balance)} left to pay.")
        return CreditApplication.objects.create(credit_note=current, invoice=invoice, amount=amount, applied_on=date)


# ---- refunds ----

def issue_refund(
    *, source: Payment | CreditNote, date: date_type, bank_account: Account, method: Literal["CHECK", "ACH"],
    amount: Decimal, user, reference: str = "",
) -> Refund:
    require_admin(user)
    with transaction.atomic():
        settings = SalesSettings.load().require_configured()
        source = type(source).objects.select_related("customer").get(pk=source.pk)
        _check_open(source)
        _check_amount(amount)
        if method not in RefundMethod.values:
            raise ReceiptError("Choose check or ACH.")
        if bank_account.subtype != AccountSubtype.BANK:
            raise ReceiptError("Choose the bank account the refund comes from.")
        _check_not_before(date, source)
        available = payment_unapplied(source) if isinstance(source, Payment) else credit_unapplied(source)
        if amount > available:
            raise ReceiptError(f"Only {_money(available)} is available to refund from {source.number}.")
        reference = (reference or "").strip()
        if method == RefundMethod.CHECK and reference:
            from banking.checknumbers import assert_free

            assert_free(bank_account, reference, error=ReceiptError)
        number = next_number(DocType.REF)
        entry = post_entry(
            date=date, user=user, memo=f"Refund {number} {source.customer.name}", reference=reference,
            source_type="sales.refund", source_id=number,
            lines=[LineSpec(settings.ar_account, debit=amount), LineSpec(bank_account, credit=amount)],
        )
        refund = Refund.objects.create(
            number=number, customer=source.customer, date=date, bank_account=bank_account, method=method,
            reference=reference, amount=amount, journal_entry=entry, created_by=user,
            source_payment=source if isinstance(source, Payment) else None,
            source_credit_note=source if isinstance(source, CreditNote) else None,
        )
        if method == RefundMethod.CHECK and reference:
            from banking.checknumbers import record
            from banking.models import CheckStatus

            record(bank_account, reference, CheckStatus.HANDWRITTEN, refund=refund, user=user)
        return refund


# ---- voids ----

def _begin_void(model, doc, user, reason: str):
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise ReceiptError("Enter a reason.")
    current = model.objects.select_related("journal_entry").get(pk=doc.pk)
    if current.status == DocStatus.VOID:
        raise ReceiptError(f"{current.number} is already void.")
    return current, reason


def _finish_void(current, *, user, reason: str, when: date_type) -> None:
    assert_open(when)
    if current.journal_entry_id:
        void_entry(current.journal_entry, user=user, reason=reason, date=when)
    current.status = DocStatus.VOID
    current.voided_on = when
    current.void_reason = reason
    current.save()


def _void_date(current, date: date_type | None, applications, others: Sequence[date_type] = ()) -> date_type:
    """Default: the document date, or later if something already happened to it after that (an application or its
    removal, a voided refund or deposit). Voiding earlier would leave the balances for the days in between
    counting a document that no longer counts."""
    latest = max(
        [current.date, *others, *(a.applied_on for a in applications),
         *(a.removed_on for a in applications if a.removed_on)]
    )
    when = date or latest
    if when < current.date:
        raise ReceiptError(f"The void date can't be before {current.number}'s date.")
    if when < latest:
        raise ReceiptError(f"The void date can't be before {latest.isoformat()}, when something else was done with it.")
    return when


def void_payment(payment: Payment, *, user, reason: str, date: date_type | None = None) -> Payment:
    with transaction.atomic():
        current, reason = _begin_void(Payment, payment, user, reason)
        if current.deposit_id and current.deposit.status != DocStatus.VOID:
            raise ReceiptError(f"{current.number} is in deposit {current.deposit.number}. Void the deposit first.")
        if current.refunds.exclude(status=DocStatus.VOID).exists():
            raise ReceiptError(f"{current.number} has a refund drawn from it. Void the refund first.")
        active = list(current.applications.filter(removed=False))
        history = [r.voided_on for r in current.refunds.all() if r.voided_on]
        if current.deposit_id and current.deposit.voided_on:
            history.append(current.deposit.voided_on)
        when = _void_date(current, date, list(current.applications.all()), history)
        _finish_void(current, user=user, reason=reason, when=when)
        for app in active:
            app.removed, app.removed_on = True, when
            app.save()
    payment.refresh_from_db()
    return payment


def void_credit_note(credit_note: CreditNote, *, user, reason: str, date: date_type | None = None) -> CreditNote:
    with transaction.atomic():
        current, reason = _begin_void(CreditNote, credit_note, user, reason)
        if current.applications.filter(removed=False).exists():
            raise ReceiptError(f"{current.number} is applied to invoices. Remove the applications first.")
        if current.refunds.exclude(status=DocStatus.VOID).exists():
            raise ReceiptError(f"{current.number} has a refund drawn from it. Void the refund first.")
        when = _void_date(
            current, date, list(current.applications.all()),
            [r.voided_on for r in current.refunds.all() if r.voided_on])
        _finish_void(current, user=user, reason=reason, when=when)
    credit_note.refresh_from_db()
    return credit_note


def void_refund(refund: Refund, *, user, reason: str, date: date_type | None = None) -> Refund:
    with transaction.atomic():
        current, reason = _begin_void(Refund, refund, user, reason)
        when = _void_date(current, date, [])
        _finish_void(current, user=user, reason=reason, when=when)
        from banking.checknumbers import mark_voided

        mark_voided(current)
    refund.refresh_from_db()
    return refund

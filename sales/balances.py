"""Balances are computed from documents plus dated application rows, so any past date can be rebuilt."""
from datetime import date
from decimal import Decimal

from ledger.balances import money

from .models import CreditNote, DocStatus, Invoice, Payment

ZERO = Decimal("0.00")


def _counts(doc, as_of: date | None) -> bool:
    """A document counts as of a date when it exists by then and was not voided on or before it."""
    if as_of is None:
        return doc.status != DocStatus.VOID
    if doc.date > as_of:
        return False
    return not (doc.status == DocStatus.VOID and doc.voided_on is not None and doc.voided_on <= as_of)


def _active(applications, as_of: date | None):
    if as_of is None:
        return [a for a in applications if not a.removed]
    return [
        a for a in applications
        if a.applied_on <= as_of and not (a.removed and a.removed_on is not None and a.removed_on <= as_of)
    ]


def _total(amounts) -> Decimal:
    return money(sum(amounts, ZERO))


def invoice_total(invoice: Invoice) -> Decimal:
    return _total(invoice.lines.values_list("amount", flat=True))


def invoice_applied(invoice: Invoice, as_of: date | None = None) -> Decimal:
    payments = _active(invoice.payment_applications.all(), as_of)
    credits = _active(invoice.credit_applications.all(), as_of)
    return _total(a.amount for a in [*payments, *credits])


def invoice_balance(invoice: Invoice, as_of: date | None = None) -> Decimal:
    if not _counts(invoice, as_of):
        return ZERO
    return money(invoice_total(invoice) - invoice_applied(invoice, as_of))


def _refunds_drawn(refunds, as_of: date | None) -> Decimal:
    return _total(
        r.amount for r in refunds.all()
        if (r.status != DocStatus.VOID if as_of is None else _counts(r, as_of))
    )


def payment_unapplied(payment: Payment, as_of: date | None = None) -> Decimal:
    if not _counts(payment, as_of):
        return ZERO
    applied = _total(a.amount for a in _active(payment.applications.all(), as_of))
    return money(payment.amount - applied - _refunds_drawn(payment.refunds, as_of))


def credit_total(credit_note: CreditNote) -> Decimal:
    return _total(credit_note.lines.values_list("amount", flat=True))


def credit_unapplied(credit_note: CreditNote, as_of: date | None = None) -> Decimal:
    if not _counts(credit_note, as_of):
        return ZERO
    applied = _total(a.amount for a in _active(credit_note.applications.all(), as_of))
    return money(credit_total(credit_note) - applied - _refunds_drawn(credit_note.refunds, as_of))


def customer_balance(customer, as_of: date | None = None) -> Decimal:
    owed = _total(invoice_balance(i, as_of) for i in customer.invoices.all())
    paid = _total(payment_unapplied(p, as_of) for p in customer.payments.all())
    credits = _total(credit_unapplied(c, as_of) for c in customer.credit_notes.all())
    return money(owed - paid - credits)

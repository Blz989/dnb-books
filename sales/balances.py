"""Balances are computed from documents plus dated application rows, so any past date can be rebuilt."""
from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from ledger.balances import money

from .models import CreditNote, DocStatus, Invoice, Payment, Refund

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


def invoices_with_balances():
    """Invoices with what balance math reads already loaded, so a report doesn't query per invoice."""
    return Invoice.objects.select_related("customer").prefetch_related("lines", "payment_applications", "credit_applications")


# ---- SQL prefilters ----
# Most documents are settled. These narrow a queryset to the ones whose balance may be nonzero as of a date, using
# the same rules as _counts and _active, so the Python math above only runs on the few that matter. They compare
# with a half-cent tolerance because SQLite sums decimals as floats; the Python math still decides the exact value.

_MONEY = DecimalField(max_digits=14, decimal_places=2)
_TOLERANCE = Decimal("0.005")


def counts_q(as_of: date | None, prefix: str = "") -> Q:
    """SQL form of _counts."""
    if as_of is None:
        return ~Q(**{f"{prefix}status": DocStatus.VOID})
    voided = Q(**{f"{prefix}status": DocStatus.VOID, f"{prefix}voided_on__isnull": False, f"{prefix}voided_on__lte": as_of})
    return Q(**{f"{prefix}date__lte": as_of}) & ~voided


def active_q(as_of: date | None) -> Q:
    """SQL form of _active, for application rows."""
    if as_of is None:
        return Q(removed=False)
    return Q(applied_on__lte=as_of) & ~Q(removed=True, removed_on__isnull=False, removed_on__lte=as_of)


def sum_of(queryset, fk: str, field: str = "amount"):
    """Correlated sum of `field` over `queryset` rows pointing at the outer row through `fk`, 0 when none."""
    inner = queryset.filter(**{fk: OuterRef("pk")}).order_by().values(fk).annotate(_s=Sum(field)).values("_s")
    return Coalesce(Subquery(inner, output_field=_MONEY), Value(Decimal("0")), output_field=_MONEY)


def nonzero(queryset, gap):
    """Rows where the annotated expression `gap` is not (within tolerance) zero."""
    return queryset.annotate(_gap=gap).filter(Q(_gap__gt=_TOLERANCE) | Q(_gap__lt=-_TOLERANCE))


def refunds_q(as_of: date | None) -> Q:
    return counts_q(as_of) if as_of is not None else ~Q(status=DocStatus.VOID)


def invoices_with_open_balance(as_of: date | None = None):
    from .models import CreditApplication, InvoiceLine, PaymentApplication

    gap = (
        sum_of(InvoiceLine.objects.all(), "invoice")
        - sum_of(PaymentApplication.objects.filter(active_q(as_of)), "invoice")
        - sum_of(CreditApplication.objects.filter(active_q(as_of)), "invoice")
    )
    return nonzero(invoices_with_balances().filter(counts_q(as_of)), gap)


def payments_with_unapplied(as_of: date | None = None):
    from .models import PaymentApplication

    gap = (
        F("amount")
        - sum_of(PaymentApplication.objects.filter(active_q(as_of)), "payment")
        - sum_of(Refund.objects.filter(refunds_q(as_of)), "source_payment")
    )
    return nonzero(payments_with_balances().filter(counts_q(as_of)), gap)


def credit_notes_with_unapplied(as_of: date | None = None):
    from .models import CreditApplication, CreditNoteLine

    gap = (
        sum_of(CreditNoteLine.objects.all(), "credit_note")
        - sum_of(CreditApplication.objects.filter(active_q(as_of)), "credit_note")
        - sum_of(Refund.objects.filter(refunds_q(as_of)), "source_credit_note")
    )
    return nonzero(credit_notes_with_balances().filter(counts_q(as_of)), gap)


def payments_with_balances():
    return Payment.objects.select_related("customer").prefetch_related("applications", "refunds")


def credit_notes_with_balances():
    return CreditNote.objects.select_related("customer").prefetch_related("lines", "applications", "refunds")


def invoice_total(invoice: Invoice) -> Decimal:
    return _total(l.amount for l in invoice.lines.all())


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
    return _total(l.amount for l in credit_note.lines.all())


def credit_unapplied(credit_note: CreditNote, as_of: date | None = None) -> Decimal:
    if not _counts(credit_note, as_of):
        return ZERO
    applied = _total(a.amount for a in _active(credit_note.applications.all(), as_of))
    return money(credit_total(credit_note) - applied - _refunds_drawn(credit_note.refunds, as_of))


def customer_balance(customer, as_of: date | None = None) -> Decimal:
    owed = _total(invoice_balance(i, as_of) for i in invoices_with_open_balance(as_of).filter(customer=customer))
    paid = _total(payment_unapplied(p, as_of) for p in payments_with_unapplied(as_of).filter(customer=customer))
    credits = _total(
        credit_unapplied(c, as_of) for c in credit_notes_with_unapplied(as_of).filter(customer=customer))
    return money(owed - paid - credits)

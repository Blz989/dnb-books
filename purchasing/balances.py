"""Vendor-side balances, from documents plus dated application rows, so any past date can be rebuilt."""
from datetime import date
from decimal import Decimal

from django.db.models import F

from ledger.balances import money
from sales.balances import ZERO, _active, _counts, _total, active_q, counts_q, nonzero, sum_of

from .models import (
    Bill, BillExpenseLine, BillItemLine, BillPaymentApplication, VendorCredit, VendorCreditApplication,
    VendorCreditLine, VendorPayment,
)


def bills_with_balances():
    """Bills with what balance math reads already loaded, so a report doesn't query per bill."""
    return Bill.objects.select_related("vendor").prefetch_related(
        "item_lines", "expense_lines", "payment_applications", "credit_applications")


def bills_with_open_balance(as_of: date | None = None):
    """Bills whose balance may be nonzero as of a date; see sales.balances.invoices_with_open_balance."""
    gap = (
        sum_of(BillItemLine.objects.all(), "bill") + sum_of(BillExpenseLine.objects.all(), "bill")
        - sum_of(BillPaymentApplication.objects.filter(active_q(as_of)), "bill")
        - sum_of(VendorCreditApplication.objects.filter(active_q(as_of)), "bill")
    )
    return nonzero(bills_with_balances().filter(counts_q(as_of)), gap)


def vendor_payments_with_unapplied(as_of: date | None = None):
    gap = F("amount") - sum_of(BillPaymentApplication.objects.filter(active_q(as_of)), "payment")
    return nonzero(vendor_payments_with_balances().filter(counts_q(as_of)), gap)


def vendor_credits_with_unapplied(as_of: date | None = None):
    gap = (
        sum_of(VendorCreditLine.objects.all(), "credit")
        - sum_of(VendorCreditApplication.objects.filter(active_q(as_of)), "credit")
    )
    return nonzero(vendor_credits_with_balances().filter(counts_q(as_of)), gap)


def vendor_payments_with_balances():
    return VendorPayment.objects.select_related("vendor").prefetch_related("applications")


def vendor_credits_with_balances():
    return VendorCredit.objects.select_related("vendor").prefetch_related("lines", "applications")


def bill_total(bill: Bill) -> Decimal:
    return _total([l.amount for l in bill.item_lines.all()] + [l.amount for l in bill.expense_lines.all()])


def bill_applied(bill: Bill, as_of: date | None = None) -> Decimal:
    payments = _active(bill.payment_applications.all(), as_of)
    credits = _active(bill.credit_applications.all(), as_of)
    return _total(a.amount for a in [*payments, *credits])


def bill_balance(bill: Bill, as_of: date | None = None) -> Decimal:
    if not _counts(bill, as_of):
        return ZERO
    return money(bill_total(bill) - bill_applied(bill, as_of))


def vendor_payment_unapplied(payment: VendorPayment, as_of: date | None = None) -> Decimal:
    if not _counts(payment, as_of):
        return ZERO
    applied = _total(a.amount for a in _active(payment.applications.all(), as_of))
    return money(payment.amount - applied)


def credit_total(credit: VendorCredit) -> Decimal:
    return _total(l.amount for l in credit.lines.all())


def vendor_credit_unapplied(credit: VendorCredit, as_of: date | None = None) -> Decimal:
    if not _counts(credit, as_of):
        return ZERO
    applied = _total(a.amount for a in _active(credit.applications.all(), as_of))
    return money(credit_total(credit) - applied)


def vendor_balance(vendor, as_of: date | None = None) -> Decimal:
    owed = _total(bill_balance(b, as_of) for b in bills_with_open_balance(as_of).filter(vendor=vendor))
    paid = _total(vendor_payment_unapplied(p, as_of) for p in vendor_payments_with_unapplied(as_of).filter(vendor=vendor))
    credits = _total(vendor_credit_unapplied(c, as_of) for c in vendor_credits_with_unapplied(as_of).filter(vendor=vendor))
    return money(owed - paid - credits)

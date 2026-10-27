"""Tie-outs between the sales documents and the ledger. Read-only; they return problem messages."""
import calendar
from datetime import date
from decimal import Decimal

from ledger.balances import account_totals, money
from ledger.models import Account

from .balances import _counts, credit_unapplied, invoice_balance, payment_unapplied
from .models import CreditNote, DocStatus, Invoice, Payment, PaymentMethod, SalesSettings

ZERO = Decimal("0.00")


def _account_balance(account: Account, as_of: date | None) -> Decimal:
    """Debit-normal balance, which is what both A/R and Undeposited Funds are."""
    debit, credit = account_totals(start=None, end=as_of or date.max).get(account.pk, (ZERO, ZERO))
    return money(debit - credit)


def customer_balances_total(as_of: date | None = None) -> Decimal:
    owed = sum((invoice_balance(i, as_of) for i in Invoice.objects.all()), ZERO)
    paid = sum((payment_unapplied(p, as_of) for p in Payment.objects.all()), ZERO)
    credits = sum((credit_unapplied(c, as_of) for c in CreditNote.objects.all()), ZERO)
    return money(owed - paid - credits)


def is_deposited(payment: Payment, as_of: date | None = None) -> bool:
    """In a deposit that was made, and not voided, by the given date."""
    for deposit in payment.all_deposits.all():
        if _counts(deposit, as_of):
            return True
    return False


def undeposited_payments_total(as_of: date | None = None) -> Decimal:
    total = ZERO
    payments = Payment.objects.filter(method__in=(PaymentMethod.CHECK, PaymentMethod.CASH))
    for payment in payments.prefetch_related("all_deposits"):
        if _counts(payment, as_of) and not is_deposited(payment, as_of):
            total += payment.amount
    return money(total)


def ar_check(as_of: date | None = None) -> list[str]:
    settings = SalesSettings.load()
    if settings.ar_account_id is None:
        return []
    documents = customer_balances_total(as_of)
    ledger = _account_balance(settings.ar_account, as_of)
    if documents == ledger:
        return []
    return [f"Customer balances say {documents:,.2f}, the A/R account says {ledger:,.2f}."]


def undeposited_check(as_of: date | None = None) -> list[str]:
    settings = SalesSettings.load()
    if settings.undeposited_account_id is None:
        return []
    documents = undeposited_payments_total(as_of)
    ledger = _account_balance(settings.undeposited_account, as_of)
    if documents == ledger:
        return []
    return [f"Undeposited payments say {documents:,.2f}, the Undeposited Funds account says {ledger:,.2f}."]


def close_check(year: int, month: int) -> list[str]:
    """Registered with ledger.periods. Unpaid invoices and undeposited checks don't block a close; a mismatch
    between the documents and the ledger does."""
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    prefix = f"{calendar.month_name[month]} {year}: "
    return [prefix + problem for problem in ar_check(month_end) + undeposited_check(month_end)]

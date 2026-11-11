"""Tie-outs between the sales documents and the ledger. Read-only; they return problem messages."""
import calendar
from datetime import date
from decimal import Decimal

from django.db.models import Q

from ledger.balances import account_totals, money
from ledger.models import Account, EntryStatus

from .balances import (
    _counts, credit_notes_with_unapplied, credit_unapplied, invoice_balance, invoices_with_open_balance,
    payment_unapplied, payments_with_unapplied,
)
from .models import (
    CreditNote, Deposit, DocStatus, Invoice, Payment, PaymentMethod, Refund, SalesSettings,
)

ZERO = Decimal("0.00")


def _account_balance(account: Account, as_of: date | None) -> Decimal:
    """Debit-normal balance, which is what both A/R and Undeposited Funds are."""
    debit, credit = account_totals(start=None, end=as_of or date.max).get(account.pk, (ZERO, ZERO))
    return money(debit - credit)


def customer_balances_total(as_of: date | None = None) -> Decimal:
    owed = sum((invoice_balance(i, as_of) for i in invoices_with_open_balance(as_of)), ZERO)
    paid = sum((payment_unapplied(p, as_of) for p in payments_with_unapplied(as_of)), ZERO)
    credits = sum((credit_unapplied(c, as_of) for c in credit_notes_with_unapplied(as_of)), ZERO)
    return money(owed - paid - credits)


def is_deposited(payment: Payment, as_of: date | None = None) -> bool:
    """In a deposit that was made, and not voided, by the given date."""
    for deposit in payment.all_deposits.all():
        if _counts(deposit, as_of):
            return True
    return False


def _entry_counts(entry, as_of: date | None) -> bool:
    """A ledger entry is in effect by the date: it exists by then and was not voided on or before it."""
    if as_of is not None and entry.date > as_of:
        return False
    if entry.status == EntryStatus.VOID:
        reversed_on = min((r.date for r in entry.reversals.all()), default=None)
        return as_of is not None and reversed_on is not None and reversed_on > as_of
    return True


def is_cleared(payment: Payment, as_of: date | None = None) -> bool:
    """Taken out of Undeposited Funds by an existing entry (not a deposit) by the given date, and not voided since.
    If an offsetting entry of the clearing was voided, the arithmetic no longer holds and the payment is open again."""
    clearing = payment.clearing
    if clearing is None:
        return False
    entries = [clearing.journal_entry, *clearing.other_entries.all()]
    return all(_entry_counts(e, as_of) for e in entries)


def undeposited_payments_total(as_of: date | None = None) -> Decimal:
    total = ZERO
    payments = Payment.objects.filter(method__in=(PaymentMethod.CHECK, PaymentMethod.CASH))
    for payment in payments.select_related("clearing__journal_entry").prefetch_related(
            "all_deposits", "clearing__journal_entry__reversals", "clearing__other_entries__reversals"):
        if _counts(payment, as_of) and not is_deposited(payment, as_of) and not is_cleared(payment, as_of):
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


def document_check() -> list[str]:
    """Open documents whose journal entry (or an invoice's stock movement) was voided without voiding the document.
    ledger.posting blocks this now; the check catches data that got there before."""
    voided_entry = Q(journal_entry__status=EntryStatus.VOID)
    numbers = []
    for model, extra in (
        (Invoice, Q(lines__stock_movement__voided=True)), (Payment, Q()), (CreditNote, Q()), (Refund, Q()),
        (Deposit, Q()),
    ):
        numbers += list(
            model.objects.filter(status=DocStatus.OPEN).filter(voided_entry | extra).distinct()
            .order_by("number").values_list("number", flat=True))
    return [f"{number} is open but its journal entry was voided. Ask for a repair." for number in numbers]


def close_check(year: int, month: int) -> list[str]:
    """Registered with ledger.periods. Unpaid invoices and undeposited checks don't block a close; a mismatch
    between the documents and the ledger does."""
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    prefix = f"{calendar.month_name[month]} {year}: "
    return [prefix + problem for problem in ar_check(month_end) + undeposited_check(month_end)] + document_check()

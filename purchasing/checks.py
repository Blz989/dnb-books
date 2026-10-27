"""Tie-out between the purchasing documents and the A/P account. Read-only; returns problem messages."""
import calendar
from datetime import date
from decimal import Decimal

from ledger.balances import account_totals, money

from .balances import (
    bill_balance, bills_with_balances, vendor_credit_unapplied, vendor_credits_with_balances,
    vendor_payment_unapplied, vendor_payments_with_balances,
)
from .models import PurchasingSettings

ZERO = Decimal("0.00")


def vendor_balances_total(as_of: date | None = None) -> Decimal:
    owed = sum((bill_balance(b, as_of) for b in bills_with_balances()), ZERO)
    paid = sum((vendor_payment_unapplied(p, as_of) for p in vendor_payments_with_balances()), ZERO)
    credits = sum((vendor_credit_unapplied(c, as_of) for c in vendor_credits_with_balances()), ZERO)
    return money(owed - paid - credits)


def ap_ledger_balance(as_of: date | None = None) -> Decimal | None:
    """Credit-normal, shown positive. None when purchasing isn't set up."""
    settings = PurchasingSettings.load()
    if settings.ap_account_id is None:
        return None
    debit, credit = account_totals(start=None, end=as_of or date.max).get(settings.ap_account_id, (ZERO, ZERO))
    return money(credit - debit)


def ap_check(as_of: date | None = None) -> list[str]:
    ledger = ap_ledger_balance(as_of)
    if ledger is None:
        return []
    documents = vendor_balances_total(as_of)
    if documents == ledger:
        return []
    return [f"Vendor balances say {documents:,.2f}, the A/P account says {ledger:,.2f}."]


def close_check(year: int, month: int) -> list[str]:
    """Registered with ledger.periods. Open bills don't block a close; a mismatch between the documents and the
    ledger does."""
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    prefix = f"{calendar.month_name[month]} {year}: "
    return [prefix + problem for problem in ap_check(month_end)]

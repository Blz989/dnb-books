"""Checks and void steps shared by payments and credits."""
from collections.abc import Sequence
from datetime import date as date_type
from decimal import Decimal

from core.roles import require_admin
from ledger.periods import assert_open
from ledger.posting import void_entry
from sales.models import DocStatus

from ..errors import MAX_AMOUNT_TEXT, PurchasingError


def money(x: Decimal) -> str:
    return f"{x:,.2f}"


def check_amount(amount: Decimal, message: str = "Enter an amount above zero.") -> None:
    if not isinstance(amount, Decimal):
        raise TypeError("Amounts must be Decimal, never float or int.")
    if not amount.is_finite() or amount <= 0:
        raise PurchasingError(message)
    if amount > Decimal(MAX_AMOUNT_TEXT):
        raise PurchasingError("That amount is too large.")
    if amount.as_tuple().exponent < -2:
        raise PurchasingError("Amounts can have at most 2 decimal places.")


def check_not_before(date: date_type, *docs) -> None:
    for doc in docs:
        if date < doc.date:
            raise PurchasingError(f"The date can't be before {doc.number}'s date.")


def check_open(doc) -> None:
    if doc.status == DocStatus.VOID:
        raise PurchasingError(f"{doc.number} is void.")


def begin_void(model, doc, user, reason: str):
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise PurchasingError("Enter a reason.")
    current = model.objects.select_related("journal_entry").get(pk=doc.pk)
    if current.status == DocStatus.VOID:
        raise PurchasingError(f"{current.number} is already void.")
    return current, reason


def finish_void(current, *, user, reason: str, when: date_type) -> None:
    assert_open(when)
    if current.journal_entry_id:
        void_entry(current.journal_entry, user=user, reason=reason, date=when)
    current.status = DocStatus.VOID
    current.voided_on = when
    current.void_reason = reason
    current.save()


def void_date(current, date: date_type | None, applications: Sequence) -> date_type:
    """Default: the document date, or later if an application or its removal came after that. Voiding earlier
    would leave the balances for the days in between counting a document that no longer counts."""
    latest = max(
        [current.date, *(a.applied_on for a in applications), *(a.removed_on for a in applications if a.removed_on)]
    )
    when = date or latest
    if when < current.date:
        raise PurchasingError(f"The void date can't be before {current.number}'s date.")
    if when < latest:
        raise PurchasingError(f"The void date can't be before {latest.isoformat()}, when something else was done with it.")
    return when

"""The only code that creates posted journal entries."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.roles import require_admin

from .errors import (
    EntryStateError, InactiveAccountError, InvalidLineError, UnbalancedEntryError,
)
from .models import Account, AccountSubtype, EntryStatus, JournalEntry, JournalLine
from .periods import assert_open

ZERO = Decimal("0")


@dataclass(frozen=True)
class LineSpec:
    account: Account
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = ""


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


def validate_lines(lines: Sequence[LineSpec]) -> None:
    """Checks in the order the plan lists them; line numbers are 1-based."""
    for line in lines:
        for amount in (line.debit, line.credit):
            if not isinstance(amount, Decimal):
                raise TypeError("Amounts must be Decimal, never float or int.")

    if len(lines) < 2:
        raise InvalidLineError("An entry needs at least two lines.")

    for n, line in enumerate(lines, 1):
        if any(not a.is_finite() for a in (line.debit, line.credit)):
            raise InvalidLineError(f"Line {n}: enter an amount.")
        if line.debit < 0 or line.credit < 0:
            raise InvalidLineError(f"Line {n}: amounts can't be negative.")

    for n, line in enumerate(lines, 1):
        if any(a.as_tuple().exponent < -2 for a in (line.debit, line.credit)):
            raise InvalidLineError(f"Line {n}: amounts can have at most 2 decimal places.")

    for n, line in enumerate(lines, 1):
        if line.debit > 0 and line.credit > 0:
            raise InvalidLineError(f"Line {n}: enter a debit or a credit, not both.")
        if line.debit == 0 and line.credit == 0:
            raise InvalidLineError(f"Line {n}: enter an amount.")

    # Re-read from the database so a stale Account object can't post to an inactive account.
    inactive = set(
        Account.objects.filter(pk__in=[l.account.pk for l in lines], is_active=False).values_list("pk", flat=True)
    )
    for n, line in enumerate(lines, 1):
        if line.account.pk in inactive:
            raise InactiveAccountError(f"Line {n}: account {line.account.number} {line.account.name} is inactive.")

    debits = sum((l.debit for l in lines), ZERO)
    credits = sum((l.credit for l in lines), ZERO)
    if debits != credits:
        raise UnbalancedEntryError(
            f"Debits ({_money(debits)}) and credits ({_money(credits)}) must be equal. "
            f"Difference: {_money(abs(debits - credits))}."
        )


def check_inventory_lines(lines: Sequence[LineSpec], source_type: str) -> None:
    """Inventory-subtype accounts change only through stock movements (source_type items.*)."""
    if source_type.startswith("items."):
        return
    inventory = set(
        Account.objects.filter(
            pk__in=[l.account.pk for l in lines], subtype=AccountSubtype.INVENTORY
        ).values_list("pk", flat=True)
    )
    for n, line in enumerate(lines, 1):
        if line.account.pk in inventory:
            raise InvalidLineError(f"Line {n}: inventory accounts can only change through stock movements.")


def _mark_posted(entry: JournalEntry, user) -> JournalEntry:
    entry.status = EntryStatus.POSTED
    entry.posted_by = user
    entry.posted_at = timezone.now()
    entry.save()
    return entry


def post_entry(
    *,
    date: date_type,
    lines: Sequence[LineSpec],
    user,
    memo: str = "",
    reference: str = "",
    source_type: str = "",
    source_id: str = "",
    reverses: JournalEntry | None = None,
) -> JournalEntry:
    with transaction.atomic():
        validate_lines(lines)
        check_inventory_lines(lines, source_type)
        assert_open(date)
        entry = JournalEntry.objects.create(
            date=date, memo=memo, reference=reference, source_type=source_type, source_id=source_id,
            reverses=reverses, created_by=user, status=EntryStatus.DRAFT,
        )
        # One create() per line (not bulk_create) so each line reaches the audit log.
        for spec in lines:
            JournalLine.objects.create(
                entry=entry, account=spec.account, debit=spec.debit, credit=spec.credit,
                description=spec.description,
            )
        return _mark_posted(entry, user)


def post_draft(entry: JournalEntry, user) -> JournalEntry:
    with transaction.atomic():
        current = JournalEntry.objects.get(pk=entry.pk)
        if current.status != EntryStatus.DRAFT:
            raise EntryStateError("Only draft entries can be posted.")
        specs = [
            LineSpec(l.account, l.debit, l.credit, l.description)
            for l in current.lines.select_related("account")
        ]
        validate_lines(specs)
        check_inventory_lines(specs, current.source_type)
        assert_open(current.date)
        _mark_posted(current, user)
    entry.refresh_from_db()
    return entry


def void_entry(
    entry: JournalEntry, *, user, reason: str, date: date_type | None = None, allow_items_source: bool = False
) -> JournalEntry:
    """Void by posting a reversing entry; the original stays on record as VOID."""
    require_admin(user)
    reason = (reason or "").strip()
    with transaction.atomic():
        current = JournalEntry.objects.get(pk=entry.pk)
        if current.status != EntryStatus.POSTED:
            raise EntryStateError("Only posted entries can be voided.")
        if not reason:
            raise ValueError("A reason is required to void an entry.")
        if current.source_type.startswith("items.") and not allow_items_source:
            raise EntryStateError("This entry was created by a stock movement. Void the movement instead.")
        reversal = post_entry(
            date=date or current.date,
            user=user,
            memo=f"Void of {current.display_number}: {reason}",
            source_type=current.source_type,
            source_id=current.source_id,
            reverses=current,
            lines=[
                LineSpec(l.account, debit=l.credit, credit=l.debit, description=l.description)
                for l in current.lines.select_related("account")
            ],
        )
        current.status = EntryStatus.VOID
        current.save()
    entry.refresh_from_db()
    return reversal

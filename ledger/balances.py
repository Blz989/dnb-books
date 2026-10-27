from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

from django.db.models import Sum

from .models import AccountType, JournalLine

CENT = Decimal("0.01")
DEBIT_NORMAL = {AccountType.ASSET, AccountType.EXPENSE}


def money(value) -> Decimal:
    """Quantize to cents. SQLite sums in floating point, so go through str()."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_EVEN)


def signed_balance(account_type: AccountType, debit: Decimal, credit: Decimal) -> Decimal:
    """Balance in the account's normal direction (assets and expenses: debit minus credit)."""
    if AccountType(account_type) in DEBIT_NORMAL:
        return money(debit - credit)
    return money(credit - debit)


def ledger_lines(*, start: date | None, end: date):
    """Posted lines (including voided originals) with start <= entry date <= end."""
    lines = JournalLine.objects.in_ledger().filter(entry__date__lte=end)
    if start is not None:
        lines = lines.filter(entry__date__gte=start)
    return lines


def account_totals(*, start: date | None, end: date) -> dict[int, tuple[Decimal, Decimal]]:
    rows = ledger_lines(start=start, end=end).values("account_id").annotate(d=Sum("debit"), c=Sum("credit"))
    return {r["account_id"]: (money(r["d"]), money(r["c"])) for r in rows}

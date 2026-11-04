"""Home page charts: cash in and out of the bank accounts, and income vs expense (accrual), by month."""
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from ledger.balances import account_totals, money
from ledger.models import AccountSubtype, AccountType, JournalLine

from . import charts

ZERO = Decimal("0.00")


@dataclass
class Month:
    start: date
    end: date
    label: str
    a: Decimal = ZERO  # cash in / income
    b: Decimal = ZERO  # cash out / expense
    balance: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.a - self.b


def last_months(today: date, count: int = 12) -> list[Month]:
    y, m = today.year, today.month
    out = []
    for _ in range(count):
        end = date(y, m, calendar.monthrange(y, m)[1])
        out.append(Month(date(y, m, 1), min(end, today), f"{date(y, m, 1):%b}"))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def cash_flow(today: date) -> dict:
    """Money in and out of bank accounts each month. Each entry is netted first, so a transfer between two
    bank accounts counts as neither."""
    months = last_months(today)
    bank = JournalLine.objects.in_ledger().filter(account__subtype=AccountSubtype.BANK)
    before = bank.filter(entry__date__lt=months[0].start).aggregate(d=Sum("debit"), c=Sum("credit"))
    running = money(before["d"]) - money(before["c"])
    per_entry = (bank.filter(entry__date__gte=months[0].start, entry__date__lte=today)
                 .values("entry_id", "entry__date").annotate(d=Sum("debit"), c=Sum("credit")))
    for row in per_entry:
        net = money(row["d"]) - money(row["c"])
        for mo in months:
            if mo.start <= row["entry__date"] <= mo.end:
                if net > 0:
                    mo.a += net
                else:
                    mo.b += -net
                break
    for mo in months:
        running += mo.net
        mo.balance = running
    tips = [charts.money_tip(f"{mo.start:%b %Y}", [("In", mo.a), ("Out", mo.b), ("Ending cash", mo.balance)])
            for mo in months]
    svg = charts.grouped_bars([mo.label for mo in months], [
        charts.Series("Cash in", "s1", [mo.a for mo in months]),
        charts.Series("Cash out", "s2", [mo.b for mo in months]),
    ], tips, "Cash in and out by month")
    return {"months": months, "svg": svg, "total_in": sum((m.a for m in months), ZERO),
            "total_out": sum((m.b for m in months), ZERO), "ending": running}


def income_expense(today: date) -> dict:
    """Income and expense (accrual) each month, from posted lines. Expense includes cost of goods sold."""
    months = last_months(today)
    from ledger.models import Account

    types = dict(Account.objects.values_list("pk", "type"))
    for mo in months:
        for account_id, (d, c) in account_totals(start=mo.start, end=mo.end).items():
            if types.get(account_id) == AccountType.INCOME:
                mo.a += c - d
            elif types.get(account_id) == AccountType.EXPENSE:
                mo.b += d - c
    tips = [charts.money_tip(f"{mo.start:%b %Y}", [("Income", mo.a), ("Expense", mo.b), ("Net", mo.net)])
            for mo in months]
    svg = charts.grouped_bars([mo.label for mo in months], [
        charts.Series("Income", "s1", [mo.a for mo in months]),
        charts.Series("Expense", "s2", [mo.b for mo in months]),
    ], tips, "Income and expense by month")
    return {"months": months, "svg": svg, "total_income": sum((m.a for m in months), ZERO),
            "total_expense": sum((m.b for m in months), ZERO)}

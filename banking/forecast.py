"""Cash forecast: cash on hand today, plus open invoices when each customer usually pays, less open bills when due.

Starting cash is read from posted ledger lines (bank accounts and Undeposited Funds). Upcoming money comes from
open documents, because it hasn't reached the ledger yet. Nothing recurring (rent, payroll) is included unless it has
been entered as a bill.
"""
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from ledger.balances import money
from ledger.models import AccountSubtype, JournalLine
from purchasing.reports import bills_due
from sales.models import DocStatus, PaymentApplication, SalesSettings
from sales.reports import open_invoices

ZERO = Decimal("0.00")
HISTORY_DAYS = 365

# (key, label, number of periods)
VIEWS = {"daily": ("Daily", 30), "weekly": ("Weekly", 13), "monthly": ("Monthly", 12)}


@dataclass
class Flow:
    expected: date
    kind: str  # "in" or "out"
    amount: Decimal
    number: str
    party: str
    due: date
    note: str = ""
    url_name: str = ""


@dataclass
class Period:
    start: date
    end: date
    label: str
    opening: Decimal = ZERO
    cash_in: Decimal = ZERO
    cash_out: Decimal = ZERO

    @property
    def net(self) -> Decimal:
        return self.cash_in - self.cash_out

    @property
    def closing(self) -> Decimal:
        return self.opening + self.net


@dataclass
class Forecast:
    today: date
    view: str
    bank_balance: Decimal
    undeposited: Decimal
    periods: list[Period]
    flows: list[Flow]
    late_receivables: Decimal
    beyond_in: Decimal
    beyond_out: Decimal
    pay_habits: dict = field(default_factory=dict)

    @property
    def starting_cash(self) -> Decimal:
        return self.bank_balance + self.undeposited

    @property
    def ending_cash(self) -> Decimal:
        return self.periods[-1].closing if self.periods else self.starting_cash

    @property
    def low_point(self) -> Period | None:
        return min(self.periods, key=lambda p: p.closing) if self.periods else None

    @property
    def total_in(self) -> Decimal:
        return sum((p.cash_in for p in self.periods), ZERO)

    @property
    def total_out(self) -> Decimal:
        return sum((p.cash_out for p in self.periods), ZERO)


def _balance(subtype_q) -> Decimal:
    row = JournalLine.objects.in_ledger().filter(subtype_q).aggregate(d=Sum("debit"), c=Sum("credit"))
    return money(row["d"]) - money(row["c"])


def cash_on_hand(today: date) -> tuple[Decimal, Decimal]:
    """(bank balance, undeposited funds) from posted lines dated on or before today."""
    from django.db.models import Q

    bank = _balance(Q(account__subtype=AccountSubtype.BANK, entry__date__lte=today))
    settings = SalesSettings.load()
    undeposited = ZERO
    if settings.undeposited_account_id:
        undeposited = _balance(Q(account_id=settings.undeposited_account_id, entry__date__lte=today))
    return bank, undeposited


def pay_habits(today: date) -> dict[int, int]:
    """Customer id -> average days paid after the due date, weighted by amount, over the last year.
    Early payment counts as negative days. Customers with no payments in the year are left out."""
    since = today - timedelta(days=HISTORY_DAYS)
    weighted = defaultdict(lambda: [ZERO, ZERO])
    apps = (PaymentApplication.objects.filter(removed=False, payment__date__gte=since, payment__date__lte=today)
            .exclude(payment__status=DocStatus.VOID)
            .select_related("payment", "invoice"))
    for a in apps:
        days = (a.payment.date - a.invoice.due_date).days
        w = weighted[a.invoice.customer_id]
        w[0] += a.amount * days
        w[1] += a.amount
    return {cid: int(round(total / amount)) for cid, (total, amount) in weighted.items() if amount > 0}


def _periods(today: date, view: str) -> list[Period]:
    _, count = VIEWS[view]
    out = []
    if view == "daily":
        for n in range(count):
            d = today + timedelta(days=n)
            out.append(Period(d, d, "Today" if n == 0 else f"{d:%a} {d:%b} {d.day}"))
    elif view == "weekly":
        start = today
        for n in range(count):
            end = start + timedelta(days=6 - start.weekday())  # through Sunday
            out.append(Period(start, end, "This week" if n == 0 else f"Week of {start:%b} {start.day}"))
            start = end + timedelta(days=1)
    else:
        start = today
        for n in range(count):
            end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
            out.append(Period(start, end, f"{start:%b %Y}"))
            start = end + timedelta(days=1)
    return out


def build(today: date, view: str = "weekly") -> Forecast:
    if view not in VIEWS:
        view = "weekly"
    bank, undeposited = cash_on_hand(today)
    habits = pay_habits(today)
    flows: list[Flow] = []
    late = ZERO
    for row in open_invoices(today):
        inv = row.invoice
        shift = habits.get(inv.customer_id)
        expected = inv.due_date + timedelta(days=shift or 0)
        note = f"Usually pays {abs(shift)} days {'late' if shift > 0 else 'early'}" if shift else "Due date"
        if expected < today:
            # Later than this customer usually pays. Count it as coming in now, but call it out.
            late += row.balance
            note = "Past the expected date"
            expected = today
        flows.append(Flow(expected, "in", row.balance, inv.number, inv.customer.name, inv.due_date, note,
                          "sales:invoice-detail"))
    for row in bills_due(through=date(9999, 12, 31), today=today):
        bill = row.bill
        expected = max(bill.due_date, today)
        note = "Overdue" if bill.due_date < today else "Due date"
        flows.append(Flow(expected, "out", row.balance, bill.number, bill.vendor.name, bill.due_date, note,
                          "purchasing:bill-detail"))
    flows.sort(key=lambda f: (f.expected, f.kind, f.number))

    periods = _periods(today, view)
    horizon = periods[-1].end
    running = bank + undeposited
    beyond_in = beyond_out = ZERO
    for p in periods:
        p.opening = running
        for f in flows:
            if p.start <= f.expected <= p.end:
                if f.kind == "in":
                    p.cash_in += f.amount
                else:
                    p.cash_out += f.amount
        running = p.closing
    for f in flows:
        if f.expected > horizon:
            if f.kind == "in":
                beyond_in += f.amount
            else:
                beyond_out += f.amount
    return Forecast(today, view, bank, undeposited, periods, [f for f in flows if f.expected <= horizon],
                    late, beyond_in, beyond_out, habits)

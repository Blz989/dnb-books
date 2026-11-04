"""Year-over-year comparison: the same report run for the same dates in earlier years, merged by account.

Each column is one run of the existing report function, so comparisons read only from posted ledger lines too.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .csv_export import amt, csv_response
from .reports import ZERO, balance_sheet, profit_and_loss, trial_balance

COMPARE_CHOICES = [("0", "No comparison"), ("1", "Previous year"), ("2", "Previous 2 years"), ("3", "Previous 3 years")]


def shift_years(d: date, years: int) -> date:
    """Same day `years` earlier; Feb 29 becomes Feb 28 in a non-leap year."""
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


@dataclass
class Row:
    label: str
    amounts: list[Decimal]
    number: str = ""
    kind: str = "row"  # row, section, total

    @property
    def change(self) -> Decimal | None:
        if self.kind == "section" or len(self.amounts) < 2:
            return None
        return self.amounts[0] - self.amounts[1]

    @property
    def percent(self) -> Decimal | None:
        if self.change is None or self.amounts[1] == 0:
            return None
        return (self.change / abs(self.amounts[1]) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


@dataclass
class Comparison:
    title: str
    columns: list[str]
    rows: list[Row] = field(default_factory=list)
    note: str = ""
    first_header: str = "Account"

    def section(self, label):
        self.rows.append(Row(label, [], kind="section"))

    def accounts(self, per_column: list[list]):
        """per_column[i] is a list of ReportRow for column i. One output row per account, ordered by number."""
        accounts = {}
        for rows in per_column:
            for r in rows:
                accounts[r.account.pk] = r.account
        lookup = [{r.account.pk: r.amount for r in rows} for rows in per_column]
        for account in sorted(accounts.values(), key=lambda a: a.number):
            self.rows.append(Row(account.name, [m.get(account.pk, ZERO) for m in lookup], number=account.number))

    def total(self, label, amounts):
        self.rows.append(Row(label, list(amounts), kind="total"))

    def line(self, label, amounts):
        self.rows.append(Row(label, list(amounts)))


def _range_label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


def _as_of_label(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def profit_and_loss_comparison(start: date, end: date, years: int, basis: str = "accrual") -> Comparison:
    from .cash_basis import profit_and_loss_cash

    run = profit_and_loss_cash if basis == "cash" else profit_and_loss
    periods = [(shift_years(start, n), shift_years(end, n)) for n in range(years + 1)]
    reports = [run(s, e) for s, e in periods]
    c = Comparison("Profit and loss", [_range_label(s, e) for s, e in periods])
    c.section("Income")
    c.accounts([r.income for r in reports])
    c.section("Cost of goods sold")
    c.accounts([r.cost_of_goods_sold for r in reports])
    c.total("Gross profit", [r.gross_profit for r in reports])
    c.section("Operating expenses")
    c.accounts([r.operating_expenses for r in reports])
    c.section("Other income")
    c.accounts([r.other_income for r in reports])
    c.section("Other expenses")
    c.accounts([r.other_expenses for r in reports])
    c.total("Net income", [r.net_income for r in reports])
    return c


def balance_sheet_comparison(as_of: date, years: int) -> Comparison:
    dates = [shift_years(as_of, n) for n in range(years + 1)]
    reports = [balance_sheet(d) for d in dates]
    c = Comparison("Balance sheet", [_as_of_label(d) for d in dates])
    c.section("Assets")
    c.accounts([r.assets for r in reports])
    c.total("Total assets", [r.total_assets for r in reports])
    c.section("Liabilities")
    c.accounts([r.liabilities for r in reports])
    c.section("Equity")
    c.accounts([r.equity for r in reports])
    c.line("Retained earnings", [r.retained_earnings for r in reports])
    c.line("Current year earnings", [r.current_year_earnings for r in reports])
    c.total("Total liabilities and equity", [r.total_liabilities_and_equity for r in reports])
    return c


@dataclass(frozen=True)
class _Net:
    account: object
    amount: Decimal


def trial_balance_comparison(as_of: date, years: int) -> Comparison:
    dates = [shift_years(as_of, n) for n in range(years + 1)]
    reports = [trial_balance(d) for d in dates]
    c = Comparison("Trial balance", [_as_of_label(d) for d in dates],
                   note="Debit balances are positive and credit balances negative.")
    c.accounts([[_Net(r.account, r.debit - r.credit) for r in tb.rows] for tb in reports])
    c.total("Total debits", [tb.total_debit for tb in reports])
    c.total("Total credits", [tb.total_credit for tb in reports])
    return c


def comparison_csv(c: Comparison, filename: str):
    header = ["Account", "Name", *c.columns]
    if len(c.columns) > 1:
        header += ["Change", "Change %"]
    rows = []
    for r in c.rows:
        if r.kind == "section":
            rows.append(["", r.label])
            continue
        row = [r.number, r.label, *[amt(a) for a in r.amounts]]
        if len(c.columns) > 1:
            row += [amt(r.change), "" if r.percent is None else f"{r.percent}"]
        rows.append(row)
    return csv_response(filename, header, rows)

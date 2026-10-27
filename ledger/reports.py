"""Reports read only from posted ledger lines and return plain dataclasses."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .balances import account_totals, ledger_lines, money, signed_balance
from .models import Account, AccountSubtype, AccountType

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ReportRow:
    account: Account
    amount: Decimal


@dataclass(frozen=True)
class TBRow:
    account: Account
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True)
class TrialBalance:
    as_of: date
    rows: list[TBRow]
    total_debit: Decimal
    total_credit: Decimal


@dataclass(frozen=True)
class GLLine:
    date: date
    entry_number: str
    reference: str
    description: str
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True)
class GeneralLedger:
    account: Account
    start: date
    end: date
    opening_balance: Decimal
    lines: list[GLLine]
    closing_balance: Decimal


@dataclass(frozen=True)
class ProfitAndLoss:
    start: date
    end: date
    income: list[ReportRow]
    cost_of_goods_sold: list[ReportRow]
    operating_expenses: list[ReportRow]
    other_income: list[ReportRow]
    other_expenses: list[ReportRow]
    gross_profit: Decimal
    net_income: Decimal


@dataclass(frozen=True)
class BalanceSheet:
    as_of: date
    assets: list[ReportRow]
    liabilities: list[ReportRow]
    equity: list[ReportRow]
    retained_earnings: Decimal
    current_year_earnings: Decimal
    total_assets: Decimal
    total_liabilities_and_equity: Decimal


def _accounts(ids) -> dict[int, Account]:
    return {a.pk: a for a in Account.objects.filter(pk__in=ids).order_by("number")}


def trial_balance(as_of: date) -> TrialBalance:
    totals = account_totals(start=None, end=as_of)
    accounts = _accounts(totals)
    rows = []
    for account in accounts.values():
        debit, credit = totals[account.pk]
        net = debit - credit
        if net > 0:
            rows.append(TBRow(account, money(net), ZERO))
        elif net < 0:
            rows.append(TBRow(account, ZERO, money(-net)))
    return TrialBalance(
        as_of=as_of,
        rows=rows,
        total_debit=money(sum((r.debit for r in rows), ZERO)),
        total_credit=money(sum((r.credit for r in rows), ZERO)),
    )


def general_ledger(account: Account, start: date, end: date) -> GeneralLedger:
    before = ledger_lines(start=None, end=start - timedelta(days=1)).filter(account=account)
    opening_debit = sum((l.debit for l in before), ZERO)
    opening_credit = sum((l.credit for l in before), ZERO)
    opening = signed_balance(account.type, opening_debit, opening_credit)

    running = opening
    out = []
    in_range = (
        ledger_lines(start=start, end=end)
        .filter(account=account)
        .select_related("entry")
        .order_by("entry__date", "entry_id", "id")
    )
    for line in in_range:
        running += signed_balance(account.type, line.debit, line.credit)
        out.append(GLLine(
            date=line.entry.date,
            entry_number=line.entry.display_number,
            reference=line.entry.reference,
            description=line.description or line.entry.memo,
            debit=money(line.debit),
            credit=money(line.credit),
            balance=money(running),
        ))
    return GeneralLedger(account, start, end, opening, out, money(running))


def _net_income(start: date | None, end: date) -> Decimal:
    totals = account_totals(start=start, end=end)
    accounts = _accounts(totals)
    net = ZERO
    for account in accounts.values():
        debit, credit = totals[account.pk]
        if account.type == AccountType.INCOME:
            net += credit - debit
        elif account.type == AccountType.EXPENSE:
            net -= debit - credit
    return money(net)


def _section(accounts, totals, subtype) -> list[ReportRow]:
    rows = []
    for account in accounts.values():
        if account.subtype != subtype:
            continue
        amount = signed_balance(account.type, *totals[account.pk])
        if amount != 0:
            rows.append(ReportRow(account, amount))
    return rows


def profit_and_loss(start: date, end: date) -> ProfitAndLoss:
    totals = account_totals(start=start, end=end)
    accounts = _accounts(totals)
    income = _section(accounts, totals, AccountSubtype.INCOME)
    cogs = _section(accounts, totals, AccountSubtype.COST_OF_GOODS_SOLD)
    opex = _section(accounts, totals, AccountSubtype.EXPENSE)
    other_income = _section(accounts, totals, AccountSubtype.OTHER_INCOME)
    other_expenses = _section(accounts, totals, AccountSubtype.OTHER_EXPENSE)

    def total(rows):
        return sum((r.amount for r in rows), ZERO)

    gross = total(income) - total(cogs)
    net = gross - total(opex) + total(other_income) - total(other_expenses)
    return ProfitAndLoss(start, end, income, cogs, opex, other_income, other_expenses, money(gross), money(net))


def balance_sheet(as_of: date) -> BalanceSheet:
    totals = account_totals(start=None, end=as_of)
    accounts = _accounts(totals)

    def rows_for(account_type):
        rows = []
        for account in accounts.values():
            if account.type == account_type:
                amount = signed_balance(account.type, *totals[account.pk])
                if amount != 0:
                    rows.append(ReportRow(account, amount))
        return rows

    assets, liabilities, equity = rows_for(AccountType.ASSET), rows_for(AccountType.LIABILITY), rows_for(AccountType.EQUITY)

    # No closing entries are posted, so earnings are derived: prior years roll into
    # retained earnings, the current calendar year stays separate.
    year_start = date(as_of.year, 1, 1)
    retained = _net_income(None, year_start - timedelta(days=1))
    current = _net_income(year_start, as_of)

    def total(rows):
        return sum((r.amount for r in rows), ZERO)

    return BalanceSheet(
        as_of=as_of,
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        retained_earnings=retained,
        current_year_earnings=current,
        total_assets=money(total(assets)),
        total_liabilities_and_equity=money(total(liabilities) + total(equity) + retained + current),
    )

from datetime import date
from decimal import Decimal

import pytest

from ledger.balances import signed_balance
from ledger.models import Account, AccountSubtype, AccountType
from ledger.posting import LineSpec, post_entry
from ledger.reports import balance_sheet, general_ledger, profit_and_loss, trial_balance

D = Decimal


@pytest.mark.parametrize("type_,debit,credit,expected", [
    (AccountType.ASSET, D("10"), D("3"), D("7")),
    (AccountType.EXPENSE, D("10"), D("3"), D("7")),
    (AccountType.LIABILITY, D("3"), D("10"), D("7")),
    (AccountType.EQUITY, D("3"), D("10"), D("7")),
    (AccountType.INCOME, D("3"), D("10"), D("7")),
])
def test_signed_balance(type_, debit, credit, expected):
    assert signed_balance(type_, debit, credit) == expected


def test_trial_balance_balances(sample_ledger):
    tb = trial_balance(date(2026, 1, 31))
    assert tb.total_debit == tb.total_credit == D("11500.00")


def test_trial_balance_includes_boundary_day(sample_ledger):
    assert trial_balance(date(2025, 12, 31)).total_debit == D("11000.00")


def test_trial_balance_rows_sorted_and_zero_balances_omitted(sample_ledger, ap):
    tb = trial_balance(date(2026, 1, 31))
    numbers = [r.account.number for r in tb.rows]
    assert numbers == sorted(numbers)
    assert ap.number not in numbers  # never used
    cash_row = next(r for r in tb.rows if r.account.number == "1000")
    assert (cash_row.debit, cash_row.credit) == (D("10700.00"), D("0.00"))


def test_pnl_range_inclusive(sample_ledger):
    p = profit_and_loss(date(2026, 1, 1), date(2026, 1, 31))
    assert p.net_income == D("200.00")
    assert [(r.account.number, r.amount) for r in p.income] == [("4000", D("500.00"))]
    assert [(r.account.number, r.amount) for r in p.operating_expenses] == [("6000", D("300.00"))]


def test_pnl_sections_and_gross_profit(sample_ledger, admin_user, cash):
    cogs = Account.objects.create(number="5000", name="COGS", type=AccountType.EXPENSE,
                                  subtype=AccountSubtype.COST_OF_GOODS_SOLD)
    other_inc = Account.objects.create(number="4900", name="Interest", type=AccountType.INCOME,
                                       subtype=AccountSubtype.OTHER_INCOME)
    other_exp = Account.objects.create(number="7000", name="Fees", type=AccountType.EXPENSE,
                                       subtype=AccountSubtype.OTHER_EXPENSE)
    for acct, side in ((cogs, "debit"), (other_exp, "debit"), (other_inc, "credit")):
        a = {side: D("40.00")}
        b = {"credit" if side == "debit" else "debit": D("40.00")}
        post_entry(date=date(2026, 1, 10), user=admin_user, lines=[LineSpec(acct, **a), LineSpec(cash, **b)])
    p = profit_and_loss(date(2026, 1, 1), date(2026, 1, 31))
    assert [r.amount for r in p.cost_of_goods_sold] == [D("40.00")]
    assert [r.amount for r in p.other_income] == [D("40.00")]
    assert [r.amount for r in p.other_expenses] == [D("40.00")]
    assert p.gross_profit == D("460.00")
    assert p.net_income == D("160.00")  # 460 gross - 300 opex + 40 other income - 40 other expense


def test_balance_sheet_balances_across_year_end(sample_ledger):
    bs = balance_sheet(date(2026, 1, 31))
    assert bs.retained_earnings == D("1000.00")
    assert bs.current_year_earnings == D("200.00")
    assert bs.total_assets == bs.total_liabilities_and_equity == D("11200.00")


def test_balance_sheet_mid_prior_year(sample_ledger):
    bs = balance_sheet(date(2025, 12, 31))
    assert bs.retained_earnings == D("0.00")
    assert bs.current_year_earnings == D("1000.00")
    assert bs.total_assets == bs.total_liabilities_and_equity == D("11000.00")


def test_general_ledger_running_balance(sample_ledger, cash):
    gl = general_ledger(cash, date(2026, 1, 1), date(2026, 1, 31))
    assert gl.opening_balance == D("11000.00") and gl.closing_balance == D("10700.00")
    assert gl.lines[-1].balance == gl.closing_balance
    keys = [(l.date, l.entry_number) for l in gl.lines]
    assert keys == sorted(keys)


def test_draft_and_void_have_no_effect(sample_ledger, cash, revenue):
    tb = trial_balance(date(2026, 1, 31))
    by_number = {r.account.number: r for r in tb.rows}
    assert by_number["1000"].debit == D("10700.00")      # 9,999 draft excluded, 50 void netted out
    assert by_number["4000"].credit == D("1500.00")
    assert by_number["6000"].debit == D("300.00")
    gl = general_ledger(cash, date(2026, 1, 1), date(2026, 1, 31))
    assert all(l.debit != D("9999.00") for l in gl.lines)


def test_report_totals_are_exact_to_the_cent(admin_user, cash, revenue):
    for _ in range(30):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("0.10")), LineSpec(revenue, credit=D("0.10"))])
    assert trial_balance(date(2026, 1, 31)).total_debit == D("3.00")

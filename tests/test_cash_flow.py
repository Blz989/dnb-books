from datetime import date
from decimal import Decimal

import pytest

from ledger.cash_flow import cash_flow
from ledger.models import Account, AccountSubtype as S, AccountType as T
from ledger.posting import LineSpec, post_entry, void_entry

D = Decimal


@pytest.fixture
def books(admin_user, cash, ar, ap, equity, revenue, expense):
    def acct(number, name, type_, subtype):
        return Account.objects.create(number=number, name=name, type=type_, subtype=subtype)

    a = {
        "cash": cash, "ar": ar, "ap": ap, "equity": equity, "sales": revenue, "rent": expense,
        "savings": acct("1010", "Savings", T.ASSET, S.BANK),
        "inventory": acct("1300", "Inventory", T.ASSET, S.INVENTORY),
        "prepaid": acct("1400", "Prepaid", T.ASSET, S.OTHER_CURRENT_ASSET),
        "truck": acct("1500", "Truck", T.ASSET, S.FIXED_ASSET),
        "accum": acct("1590", "Accumulated depreciation", T.ASSET, S.FIXED_ASSET),
        "deposit": acct("1800", "Security deposit", T.ASSET, S.OTHER_ASSET),
        "visa": acct("2100", "Visa", T.LIABILITY, S.CREDIT_CARD),
        "accrued": acct("2200", "Accrued", T.LIABILITY, S.OTHER_CURRENT_LIABILITY),
        "loan": acct("2700", "Truck loan", T.LIABILITY, S.LONG_TERM_LIABILITY),
        "cogs": acct("5000", "COGS", T.EXPENSE, S.COST_OF_GOODS_SOLD),
        "depr": acct("6400", "Depreciation", T.EXPENSE, S.EXPENSE),
        "interest": acct("4800", "Interest income", T.INCOME, S.OTHER_INCOME),
    }

    def post(day, debit, credit, amount):
        # Inventory changes only through stock movements; this stands in for one.
        source = "items.movement" if "inventory" in (debit, credit) else ""
        return post_entry(date=day, user=admin_user, source_type=source, lines=[
            LineSpec(a[debit], debit=D(amount)), LineSpec(a[credit], credit=D(amount))])

    post(date(2025, 12, 1), "cash", "equity", "50000.00")        # before the range: opening cash
    post(date(2026, 1, 2), "inventory", "ap", "8000.00")
    post(date(2026, 1, 5), "ar", "sales", "6000.00")
    post(date(2026, 1, 6), "cogs", "inventory", "3500.00")
    post(date(2026, 1, 10), "cash", "ar", "4000.00")
    post(date(2026, 1, 12), "ap", "cash", "5000.00")
    post(date(2026, 1, 14), "rent", "visa", "900.00")
    post(date(2026, 1, 15), "prepaid", "cash", "1200.00")
    post(date(2026, 1, 16), "rent", "accrued", "300.00")
    post(date(2026, 1, 18), "truck", "cash", "10000.00")
    post(date(2026, 1, 19), "truck", "loan", "20000.00")          # non-cash: financed purchase
    post(date(2026, 1, 20), "deposit", "cash", "2000.00")
    post(date(2026, 1, 25), "loan", "cash", "1000.00")
    post(date(2026, 1, 26), "equity", "cash", "3000.00")          # owner draw
    post(date(2026, 1, 27), "savings", "cash", "7000.00")         # bank to bank: not a cash flow
    post(date(2026, 1, 28), "cash", "interest", "25.00")
    post(date(2026, 1, 31), "depr", "accum", "450.00")            # non-cash charge
    v = post(date(2026, 1, 30), "rent", "cash", "99.00")
    void_entry(v, user=admin_user, reason="Twice")
    post(date(2026, 2, 3), "cash", "sales", "777.00")             # after the range
    return a


def amounts(lines):
    return {l.account.number: l.amount for l in lines}


def test_sections_signs_and_tie_out(books):
    cf = cash_flow(date(2026, 1, 1), date(2026, 1, 31))
    # 6000 sales + 25 interest - 3500 cogs - 900 - 300 rent - 450 depreciation
    assert cf.net_income == D("875.00")
    assert cf.noncash == D("450.00")
    assert amounts(cf.operating) == {
        "1200": D("-2000.00"), "1300": D("-4500.00"), "1400": D("-1200.00"),
        "2000": D("3000.00"), "2100": D("900.00"), "2200": D("300.00")}
    assert cf.total_operating == D("-2175.00")
    assert amounts(cf.investing) == {"1500": D("-30000.00"), "1800": D("-2000.00")}
    assert amounts(cf.financing) == {"2700": D("19000.00"), "3000": D("-3000.00")}
    assert cf.total_investing == D("-32000.00") and cf.total_financing == D("16000.00")
    assert cf.net_change == D("-18175.00")
    assert cf.cash_start == D("50000.00") and cf.cash_end == D("31825.00")
    assert cf.bank_change == D("-18175.00") and cf.difference == D("0.00")


def test_empty_range(books):
    cf = cash_flow(date(2027, 1, 1), date(2027, 1, 31))
    assert cf.operating == [] and cf.investing == [] and cf.financing == []
    assert cf.net_change == D("0.00") and cf.difference == D("0.00")
    assert cf.cash_start == cf.cash_end == D("32602.00")


def test_whole_history_ties(books):
    assert cash_flow(date(2020, 1, 1), date(2026, 12, 31)).difference == D("0.00")


def test_report_page_csv_and_catalog(client, standard_user, books):
    client.force_login(standard_user)
    html = client.get("/reports/cash-flow/?start=2026-01-01&end=2026-01-31").content.decode()
    for text in ("Cash flow statement", "Operating activities", "Investing activities", "Financing activities",
                 "Depreciation and other non-cash charges", "Difference", "31,825.00"):
        assert text in html
    resp = client.get("/reports/cash-flow/?start=2026-01-01&end=2026-01-31&format=csv")
    assert resp["Content-Type"].startswith("text/csv") and b"Net change in cash" in resp.content
    assert "Cash flow statement" in client.get("/reports/").content.decode()

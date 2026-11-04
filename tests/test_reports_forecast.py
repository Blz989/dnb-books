"""Year-over-year comparison, report PDFs, home charts, the cash forecast, and the collapsible sidebar."""
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking import forecast as fc
from contacts.models import Terms
from core import charts, dashboard
from items.models import ItemType
from ledger.comparison import (
    balance_sheet_comparison, profit_and_loss_comparison, shift_years, trial_balance_comparison,
)
from ledger.posting import LineSpec, post_entry
from sales.models import PaymentMethod
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment

D = Decimal


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


# ---- comparison ----

def test_shift_years_handles_leap_day():
    assert shift_years(date(2024, 2, 29), 1) == date(2023, 2, 28)
    assert shift_years(date(2026, 11, 3), 2) == date(2024, 11, 3)


def test_profit_and_loss_comparison_lines_up_accounts(sample_ledger):
    c = profit_and_loss_comparison(date(2026, 1, 1), date(2026, 12, 31), 1)
    assert c.columns == ["Jan 1 – Dec 31, 2026", "Jan 1 – Dec 31, 2025"]
    by_label = {r.label: r for r in c.rows if r.kind != "section"}
    revenue = next(r for r in c.rows if r.number == "4000")
    assert revenue.amounts == [D("500.00"), D("1000.00")]
    assert revenue.change == D("-500.00") and revenue.percent == D("-50.0")
    assert by_label["Net income"].amounts == [D("200.00"), D("1000.00")]


def test_percent_is_blank_when_prior_year_is_zero(sample_ledger):
    c = profit_and_loss_comparison(date(2026, 1, 1), date(2026, 12, 31), 1)
    expense = next(r for r in c.rows if r.number == "6000")
    assert expense.amounts == [D("300.00"), D("0.00")] and expense.percent is None


def test_balance_sheet_and_trial_balance_comparisons(sample_ledger):
    bs = balance_sheet_comparison(date(2026, 1, 31), 1)
    total = next(r for r in bs.rows if r.label == "Total assets")
    assert total.amounts == [D("11200.00"), D("0.00")]
    tb = trial_balance_comparison(date(2026, 1, 31), 2)
    assert len(tb.columns) == 3
    debits = next(r for r in tb.rows if r.label == "Total debits")
    assert debits.amounts[0] == D("11500.00")  # cash 10,700 + A/R 500 + expense 300


def test_comparison_views_and_csv(adm, sample_ledger):
    html = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-12-31&compare=1").content.decode()
    assert "Jan 1 – Dec 31, 2025" in html and "-50.0%" in html and "Compare with" in html
    csv = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-12-31&compare=1&format=csv")
    assert csv["Content-Type"].startswith("text/csv")
    assert "Change %" in csv.content.decode().splitlines()[0]
    assert "Jan 31, 2025" in adm.get("/reports/balance-sheet/?as_of=2026-01-31&compare=1").content.decode()
    assert "Jan 31, 2024" in adm.get("/reports/trial-balance/?as_of=2026-01-31&compare=2").content.decode()


def test_no_comparison_by_default(adm, sample_ledger):
    html = adm.get("/reports/profit-and-loss/").content.decode()
    assert "Compare with" in html and "Change compares" not in html


def test_order_profitability_compares_totals(adm, db):
    html = adm.get("/reports/order-profitability/?from=2026-01-01&to=2026-03-31&compare=1").content.decode()
    assert "Totals by year" in html and "Jan 1 – Mar 31, 2025" in html


# ---- PDF export ----

def test_report_pdf_download(adm, sample_ledger):
    page = adm.get("/reports/trial-balance/?as_of=2026-01-31").content.decode()
    assert "format=pdf" in page and "Download PDF" in page
    r = adm.get("/reports/trial-balance/?as_of=2026-01-31&format=pdf")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert 'filename="trial-balance.pdf"' in r["Content-Disposition"]


def test_report_pdf_html_has_no_screen_chrome(rf, admin_user, sample_ledger):
    from django.urls import resolve

    from ledger.views import trial_balance

    request = rf.get("/reports/trial-balance/?as_of=2026-01-31&format=pdf")
    request.user = admin_user
    request.resolver_match = resolve("/reports/trial-balance/")
    request.report_pdf = True
    html = trial_balance(request).content.decode()
    assert "<aside>" not in html and 'aria-label="Reports"' not in html and "11,500.00" in html


def test_reports_index_has_no_pdf_button(adm, db):
    assert "Download PDF" not in adm.get("/reports/").content.decode()


# ---- home charts ----

def test_cash_flow_ignores_transfers_between_bank_accounts(admin_user, cash, revenue):
    from ledger.models import Account, AccountSubtype, AccountType

    savings = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET,
                                     subtype=AccountSubtype.BANK)
    post_entry(date=date(2026, 10, 2), user=admin_user, lines=[LineSpec(cash, debit=D("500.00")),
                                                               LineSpec(revenue, credit=D("500.00"))])
    post_entry(date=date(2026, 10, 3), user=admin_user, lines=[LineSpec(savings, debit=D("200.00")),
                                                               LineSpec(cash, credit=D("200.00"))])
    data = dashboard.cash_flow(date(2026, 10, 31))
    october = data["months"][-1]
    assert october.a == D("500.00") and october.b == D("0.00") and data["ending"] == D("500.00")


def test_income_expense_by_month(sample_ledger):
    data = dashboard.income_expense(date(2026, 1, 31))
    jan = data["months"][-1]
    assert jan.a == D("500.00") and jan.b == D("300.00")  # the voided 50 nets to zero


def test_home_shows_both_charts(adm, sample_ledger):
    html = adm.get("/").content.decode()
    assert "Cash flow" in html and "Income and expense" in html and html.count('class="chart-svg"') == 2


def test_chart_svg_draws_bars_with_tooltips():
    svg = charts.grouped_bars(["Jan", "Feb"], [charts.Series("In", "s1", [D("10"), D("20")]),
                                               charts.Series("Out", "s2", [D("5"), D("0")])],
                              ["Jan · In 10.00", "Feb · In 20.00"], "Test")
    assert svg.count('class="bar s1"') == 2 and svg.count('class="bar s2"') == 1
    assert 'data-tip="Feb · In 20.00"' in svg


def test_tick_labels_never_repeat():
    from core.charts import _scale, _short

    _, ticks = _scale(88600, 89400, zero=False)
    labels = [_short(t, ticks[1] - ticks[0]) for t in ticks]
    assert len(set(labels)) == len(labels)


# ---- cash forecast ----

@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def test_pay_habits_weight_by_amount(admin_user, customer, service, cash):
    a = create_invoice(customer=customer, date=date(2026, 1, 1), user=admin_user, terms=Terms.NET_30,
                       lines=[InvoiceLineSpec(service, 1, D("300.00"))])
    b = create_invoice(customer=customer, date=date(2026, 2, 1), user=admin_user, terms=Terms.NET_30,
                       lines=[InvoiceLineSpec(service, 1, D("100.00"))])
    receive_payment(customer=customer, date=a.due_date + (date(2026, 1, 11) - date(2026, 1, 1)),
                    method=PaymentMethod.ACH, amount=D("300.00"), user=admin_user, bank_account=cash,
                    applications=[(a, D("300.00"))])
    receive_payment(customer=customer, date=b.due_date - (date(2026, 1, 3) - date(2026, 1, 1)),
                    method=PaymentMethod.ACH, amount=D("100.00"), user=admin_user, bank_account=cash,
                    applications=[(b, D("100.00"))])
    # (300 x 10 days late + 100 x 2 days early) / 400 = 7
    assert fc.pay_habits(date(2026, 6, 1)) == {customer.pk: 7}


def test_forecast_places_invoices_by_habit_and_bills_by_due_date(admin_user, customer, service, cash, revenue,
                                                                 vendor, expense, purchasing_settings):
    from purchasing.posting.bills import BillExpenseSpec, create_bill

    post_entry(date=date(2026, 3, 1), user=admin_user, lines=[LineSpec(cash, debit=D("1000.00")),
                                                              LineSpec(revenue, credit=D("1000.00"))])
    old = create_invoice(customer=customer, date=date(2026, 1, 1), user=admin_user, terms=Terms.NET_30,
                         lines=[InvoiceLineSpec(service, 1, D("50.00"))])
    receive_payment(customer=customer, date=old.due_date + (date(2026, 1, 6) - date(2026, 1, 1)),
                    method=PaymentMethod.ACH, amount=D("50.00"), user=admin_user, bank_account=cash,
                    applications=[(old, D("50.00"))])
    inv = create_invoice(customer=customer, date=date(2026, 3, 1), user=admin_user, terms=Terms.NET_30,
                         lines=[InvoiceLineSpec(service, 1, D("400.00"))])
    bill = create_bill(vendor=vendor, date=date(2026, 3, 1), vendor_invoice_number="V-1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("250.00"))])
    f = fc.build(date(2026, 3, 2), "weekly")
    assert f.bank_balance == D("1050.00")  # 1,000 posted plus the 50 ACH payment
    flows = {x.number: x for x in f.flows}
    # This customer paid 5 days late, so the new invoice is expected 5 days after its due date.
    assert flows[inv.number].expected == date(2026, 4, 5) and flows[inv.number].note == "Usually pays 5 days late"
    assert flows[bill.number].expected == bill.due_date and flows[bill.number].kind == "out"
    assert f.periods[0].opening == f.starting_cash
    assert f.periods[-1].closing == f.starting_cash + f.total_in - f.total_out


def test_forecast_late_invoice_counts_today_and_is_flagged(admin_user, customer, service, cash):
    inv = create_invoice(customer=customer, date=date(2026, 1, 1), user=admin_user, terms=Terms.NET_30,
                         lines=[InvoiceLineSpec(service, 1, D("80.00"))])
    f = fc.build(date(2026, 4, 1), "weekly")
    flow = next(x for x in f.flows if x.number == inv.number)
    assert flow.expected == date(2026, 4, 1) and flow.note == "Past the expected date"
    assert f.late_receivables == D("80.00") and f.periods[0].cash_in == D("80.00")


def test_forecast_periods(db):
    weekly = fc._periods(date(2026, 11, 4), "weekly")  # a Wednesday
    assert (weekly[0].start, weekly[0].end) == (date(2026, 11, 4), date(2026, 11, 8))
    assert weekly[1].start == date(2026, 11, 9) and len(weekly) == 13
    monthly = fc._periods(date(2026, 11, 4), "monthly")
    assert monthly[0].end == date(2026, 11, 30) and monthly[1].start == date(2026, 12, 1) and len(monthly) == 12
    assert len(fc._periods(date(2026, 11, 4), "daily")) == 30


def test_forecast_page_and_views(adm, sales_settings):
    for view in ("daily", "weekly", "monthly"):
        html = adm.get(f"/reports/cash-forecast/?view={view}").content.decode()
        assert "Cash today" in html and "Lowest point" in html and "Not included." in html
    assert adm.get("/reports/cash-forecast/?view=nonsense").status_code == 200
    r = adm.get("/reports/cash-forecast/?format=pdf")
    assert r["Content-Type"] == "application/pdf"


def test_forecast_link_in_sidebar_and_reports(adm, db):
    assert 'href="/reports/cash-forecast/"' in adm.get("/").content.decode()
    assert 'href="/reports/cash-forecast/"' in adm.get("/reports/").content.decode()


# ---- sidebar ----

def test_sidebar_groups_collapse(adm, db):
    html = adm.get("/").content.decode()
    for key in ("sales", "purchases", "banking", "inventory", "accountant"):
        assert f'<details class="grp" data-grp="{key}" open>' in html, key
    assert "dnb-nav-collapsed" in html


def test_report_groups_have_headers(adm, db):
    html = adm.get("/reports/trial-balance/").content.decode()
    assert html.count('class="report-group-h"') == 5

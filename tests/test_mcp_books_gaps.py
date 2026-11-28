"""Claude tools for the books-gaps features: reports, write-offs, and recurring schedules."""
from datetime import date
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ledger.cash_flow import cash_flow
from ledger.models import Account, AccountSubtype, AccountType, JournalEntry, RecurringJournal
from ledger.sales_reports import purchases_by_item, purchases_by_vendor, sales_by_customer, sales_by_item
from mcpserver import read, write
from mcpserver.read_gaps import list_recurring
from mcpserver.runtime import run
from purchasing.models import RecurringBill
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.balances import invoice_balance
from sales.models import CreditNote
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from tests.test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (shared fixtures)

D = Decimal
START, END = "2026-10-01", "2026-10-31"


@pytest.fixture
def activity(books, owner):
    inv = create_invoice(customer=books.customer, date=date(2026, 10, 5), user=owner,
                         lines=[InvoiceLineSpec(books.service, 2, D("100.00"))])
    create_bill(vendor=books.vendor, date=date(2026, 10, 6), vendor_invoice_number="V-9", user=owner,
                expense_lines=[BillExpenseSpec(books.freight, D("40.00"))])
    return inv


def report(user, name, **kw):
    return run(ctx_for(user), "run_report", read.run_report, report=name, start=START, end=END, **kw)


# ---- reports ----

def test_sales_by_customer_matches_screen(owner, activity):
    out = report(owner, "sales_by_customer")
    screen = sales_by_customer(date(2026, 10, 1), date(2026, 10, 31))
    assert out["rows"] == [{"customer": "Acme Builders", "invoices": 1, "sales": "200.00", "credits": "0.00",
                            "net": "200.00", "written_off": "0.00"}]
    assert out["totals"]["net"] == f"{screen.total.net:.2f}" and out["difference"] == "0.00"
    assert out["pnl_income"] == f"{screen.pnl_income:.2f}"


def test_sales_by_item_with_customer_filter(owner, activity, books):
    out = report(owner, "sales_by_item", customer="acme builders")
    assert out["rows"] == [{"sku": "SVC", "name": books.service.name, "quantity": 2, "amount": "200.00",
                            "average": "100.00"}]
    assert out["difference"] == "0.00"
    screen = sales_by_item(date(2026, 10, 1), date(2026, 10, 31), customer=books.customer)
    assert out["total"] == f"{screen.total:.2f}"


def test_unknown_customer_refused(owner, activity):
    with pytest.raises(ToolError, match="customer"):
        report(owner, "sales_by_item", customer="Nobody Inc")


def test_purchases_reports(owner, activity, books):
    out = report(owner, "purchases_by_vendor")
    assert out["rows"][0]["vendor"] == books.vendor.name and out["rows"][0]["net"] == "40.00"
    assert out["difference"] == "0.00"
    out = report(owner, "purchases_by_item", vendor=books.vendor.name)
    assert out["rows"] == [{"sku_or_account": "6150", "name": "Freight in", "quantity": None, "amount": "40.00",
                            "average": ""}]
    assert out["total"] == f"{purchases_by_item(date(2026, 10, 1), date(2026, 10, 31)).total:.2f}"
    assert purchases_by_vendor(date(2026, 10, 1), date(2026, 10, 31)).total.net == D("40.00")


def test_cash_flow_matches_screen(owner, activity):
    out = report(owner, "cash_flow")
    screen = cash_flow(date(2026, 10, 1), date(2026, 10, 31))
    assert out["net_income"] == f"{screen.net_income:.2f}"
    assert out["net_change"] == f"{screen.net_change:.2f}" and out["difference"] == "0.00"
    assert {r["account"] for r in out["operating"]} >= {"1200 Accounts Receivable", "2000 Accounts Payable"}


# ---- write-off ----

@pytest.fixture
def bad_debt(books, sales_settings):
    a = Account.objects.create(number="6900", name="Bad debt", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)
    sales_settings.bad_debt_account = a
    sales_settings.save()
    return a


def preview(user, op, payload):
    return run(ctx_for(user), op, write.preview, operation=op, payload=payload)


def confirm(user, p):
    return run(ctx_for(user), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])


def test_write_off_preview_then_confirm(owner, activity, bad_debt):
    entries = JournalEntry.objects.count()
    p = preview(owner, "write_off_invoice", {"invoice": activity.number, "reason": "Closed", "date": "2026-11-20"})
    assert CreditNote.objects.count() == 0 and JournalEntry.objects.count() == entries
    s = p["preview"]
    assert s["invoice"] == activity.number and s["amount"] == "200.00" and s["balance_after"] == "0.00"
    assert s["bad_debt_account"] == "6900 Bad debt"
    assert {"account": "6900 Bad debt", "debit": "200.00", "credit": ""} in s["posts"]
    done = confirm(owner, p)
    note = CreditNote.objects.get()
    assert done["document"] == note.number and note.write_off_of == activity
    assert invoice_balance(activity) == D("0.00")


def test_write_off_is_admin_only(clerk, activity, bad_debt):
    with pytest.raises(ToolError):
        preview(clerk, "write_off_invoice", {"invoice": activity.number, "reason": "Closed"})
    assert CreditNote.objects.count() == 0


def test_write_off_app_messages_pass_through(owner, activity, bad_debt):
    with pytest.raises(ToolError, match="Enter a reason."):
        preview(owner, "write_off_invoice", {"invoice": activity.number, "reason": ""})
    with pytest.raises(ToolError, match="left to pay"):
        preview(owner, "write_off_invoice", {"invoice": activity.number, "reason": "x", "amount": "999.00"})


# ---- recurring ----

def bill_payload(**kw):
    p = {"name": "Rent", "vendor": "Acme Supply", "frequency": "monthly", "start_date": "2026-12-01",
         "lines": [{"account": "6150", "amount": "1,200.00", "description": "Monthly"}]}
    p.update(kw)
    return p


def test_create_recurring_bill(owner, books):
    p = preview(owner, "create_recurring_bill", bill_payload(notes="Unit 4"))
    assert not RecurringBill.objects.exists()
    assert p["preview"]["amount"] == "1200.00" and p["preview"]["next_date"] == "2026-12-01"
    confirm(owner, p)
    r = RecurringBill.objects.get()
    assert (r.name, r.vendor, r.created_by, r.notes) == ("Rent", books.vendor, owner, "Unit 4")
    assert r.lines.get().amount == D("1200.00")


@pytest.mark.parametrize("change,message", [
    ({"lines": []}, "Add at least one line."),
    ({"lines": [{"account": "6150", "amount": "0"}]}, "Line 1: enter an amount above zero."),
    ({"end_date": "2026-11-01"}, "The end date can't be before the start date."),
    ({"frequency": "hourly"}, "frequency"),
])
def test_recurring_bill_refusals(owner, books, change, message):
    with pytest.raises(ToolError, match=message):
        preview(owner, "create_recurring_bill", bill_payload(**change))


def test_create_recurring_journal_entry(owner, books, cash, expense):
    payload = {"name": "Depreciation", "memo": "Truck", "frequency": "monthly", "start_date": "2026-12-31",
               "lines": [{"account": "6000", "debit": "250.00"}, {"account": "1000", "credit": "250.00"}]}
    p = preview(owner, "create_recurring_journal_entry", payload)
    assert not RecurringJournal.objects.exists()
    confirm(owner, p)
    assert RecurringJournal.objects.get().lines.count() == 2
    payload["lines"][1]["credit"] = "200.00"
    with pytest.raises(ToolError, match="must be equal"):
        preview(owner, "create_recurring_journal_entry", payload)


def test_list_pause_resume(owner, books):
    confirm(owner, preview(owner, "create_recurring_bill", bill_payload()))
    out = run(ctx_for(owner), "list_recurring", list_recurring)
    assert out["schedules"] == [{"kind": "bill", "name": "Rent", "vendor": "Acme Supply", "frequency": "Monthly",
                                 "start_date": "2026-12-01", "end_date": "", "amount": "1200.00",
                                 "next_date": "2026-12-01", "status": "Active", "last_error": ""}]
    confirm(owner, preview(owner, "pause_recurring", {"kind": "bill", "name": "rent"}))
    assert not RecurringBill.objects.get().is_active
    confirm(owner, preview(owner, "resume_recurring", {"kind": "bill", "name": "Rent"}))
    assert RecurringBill.objects.get().is_active


def test_duplicate_names_refused(owner, books):
    confirm(owner, preview(owner, "create_recurring_bill", bill_payload()))
    confirm(owner, preview(owner, "create_recurring_bill", bill_payload()))
    with pytest.raises(ToolError, match="More than one recurring bill is named Rent. Rename one in the app first."):
        preview(owner, "pause_recurring", {"kind": "bill", "name": "Rent"})

"""MCP read tools for orders, payments, credits, bank accounts, the ledger, stock movements, and month close."""
from datetime import date
from decimal import Decimal

import pytest

from banking.models import AccountKind, BankAccount
from ledger.periods import close_period
from mcpserver import read_more
from mcpserver.runtime import Refused
from sales.balances import payment_unapplied
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, create_credit_note, receive_payment
from tests.test_mcp import books, owner  # noqa: F401  (shared fixtures)

D = Decimal
DAY = date(2026, 10, 15)


@pytest.fixture
def activity(books, owner):
    order = create_order(customer=books.customer, date=DAY, user=owner,
                         lines=[OrderLineSpec(books.widget, 4, D("25.00"))])
    invoice = create_invoice(customer=books.customer, date=DAY, user=owner,
                             lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    payment = receive_payment(customer=books.customer, date=DAY, method="CHECK", amount=D("150.00"), user=owner,
                              applications=[(invoice, D("100.00"))])
    return order, invoice, payment


def test_sales_orders_list_and_detail(books, owner, activity):
    order, _, _ = activity
    listed = read_more.list_sales_orders(owner)["sales_orders"]
    assert [r["number"] for r in listed] == [order.number] and listed[0]["total"] == "100.00"
    detail = read_more.get_sales_order(owner, order.number)
    assert detail["lines"][0]["remaining"] == 4 and detail["total"] == "100.00"
    assert read_more.list_sales_orders(owner, status="closed")["sales_orders"] == []


def test_unknown_order_number_message(books, owner):
    with pytest.raises(Refused, match="No sales order has the number SO-000999."):
        read_more.get_sales_order(owner, "SO-000999")


def test_customer_payment_shows_unapplied_amount(books, owner, activity):
    _, invoice, payment = activity
    row = read_more.list_customer_payments(owner, customer=books.customer.name)["payments"][0]
    assert row["number"] == payment.number and row["unapplied"] == "50.00"
    assert D(row["unapplied"]) == payment_unapplied(payment)
    detail = read_more.get_customer_payment(owner, payment.number)
    assert detail["applied"] == [{"invoice": invoice.number, "date": "2026-10-15", "amount": "100.00"}]


def test_credit_notes_list_open_with_unapplied(books, owner, sales_settings):
    note = create_credit_note(customer=books.customer, date=DAY, user=owner, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("30.00"))])
    rows = read_more.list_credits(owner)["credits"]
    assert rows[0]["number"] == note.number and rows[0]["total"] == "30.00" and rows[0]["unapplied"] == "30.00"
    assert read_more.list_credits(owner, kind="vendor")["credits"] == []
    with pytest.raises(Refused, match="Choose a kind"):
        read_more.list_credits(owner, kind="other")


def test_bank_accounts_list_and_detail(books, owner, activity):
    BankAccount.objects.create(ledger_account=books.bank, kind=AccountKind.CHECKING, mask="1234")
    rows = read_more.list_bank_accounts(owner)
    assert rows["accounts"][0]["account"].startswith("1000") and "bank_inbox_waiting" in rows
    assert read_more.get_bank_account(owner, "1000")["mask"] == "1234"
    with pytest.raises(Refused, match="is not a bank account"):
        read_more.get_bank_account(owner, books.freight.number)


def test_account_ledger_matches_the_report(books, owner, activity):
    out = read_more.get_account_ledger(owner, "1200", start="2026-01-01", end="2026-12-31")
    assert out["lines"] and out["closing_balance"] == out["lines"][-1]["balance"]
    with pytest.raises(Refused, match="start date is after"):
        read_more.get_account_ledger(owner, "1200", start="2026-12-31", end="2026-01-01")


def test_close_checklist(books, owner):
    out = read_more.close_checklist(owner, month="2026-10")
    assert out["month"] == "October 2026" and out["closed"] is False and "problems" in out
    close_period(2026, 10, owner, override_reason="test")
    assert read_more.close_checklist(owner, month="2026-10")["closed"] is True
    with pytest.raises(Refused, match="YYYY-MM"):
        read_more.close_checklist(owner, month="October")


def test_stock_movements_list(books, owner, activity):
    out = read_more.list_stock_movements(owner, sku="WID-1", start="2026-01-01", end="2026-12-31")
    assert "movements" in out
    with pytest.raises(Refused, match="No item has SKU"):
        read_more.list_stock_movements(owner, sku="NOPE")


# ---- extra reports ----

from purchasing.posting.bills import BillExpenseSpec, create_bill  # noqa: E402
from sales import reports as sales_reports  # noqa: E402
from purchasing import reports as purchasing_reports  # noqa: E402


def test_balance_reports_match_the_screens(books, owner, activity):
    from mcpserver import read

    out = read.run_report(owner, "customer_balances", as_of="2026-12-31")
    rows = sales_reports.customer_balances(date(2026, 12, 31))
    assert out["total"] == f"{sum(r.balance for r in rows):.2f}" and out["customers"][0]["customer"] == books.customer.name
    unpaid = read.run_report(owner, "unpaid_invoices", as_of="2026-12-31")
    assert unpaid["totals"]["total"] == f"{sales_reports.unpaid_by_customer(date(2026, 12, 31)).totals.total:.2f}"
    create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1", item_lines=[],
                expense_lines=[BillExpenseSpec(books.freight, D("40.00"))])
    vb = read.run_report(owner, "vendor_balances", as_of="2026-12-31")
    assert vb["total"] == f"{sum(r.balance for r in purchasing_reports.vendor_balances(date(2026, 12, 31))):.2f}"


def test_order_and_undeposited_reports(books, owner, activity):
    from mcpserver import read

    order, _, _ = activity
    lines = read.run_report(owner, "open_sales_orders")["lines"]
    assert lines[0]["order"] == order.number and lines[0]["left"] == 4
    assert read.run_report(owner, "open_purchase_orders") == {"lines": []}
    und = read.run_report(owner, "undeposited_funds")
    assert und["total"] == "150.00" and und["payments"][0]["amount"] == "150.00"
    assert read.run_report(owner, "drop_ship_awaiting_bill") == {"lines": []}
    assert read.run_report(owner, "negative_stock") == {"items": []}


def test_ledger_register_and_1099_reports(books, owner, activity):
    from mcpserver import read

    gl = read.run_report(owner, "general_ledger", account="1200", start="2026-01-01", end="2026-12-31")
    assert gl["lines"]
    with pytest.raises(Refused, match="Choose an account for the general ledger."):
        read.run_report(owner, "general_ledger")
    with pytest.raises(Refused, match="Choose a bank account for the register."):
        read.run_report(owner, "bank_register")
    BankAccount.objects.create(ledger_account=books.bank, kind=AccountKind.CHECKING)
    reg = read.run_report(owner, "bank_register", account="1000", start="2026-01-01", end="2026-12-31")
    assert reg["account"].startswith("1000") and "rows" in reg
    assert read.run_report(owner, "1099_summary", end="2026-12-31")["year"] == 2026
    assert "movements" in read.run_report(owner, "stock_movements", start="2026-01-01", end="2026-12-31")


def test_fifo_report_needs_a_fifo_item(books, owner):
    from mcpserver import read

    with pytest.raises(Refused, match="Choose an item"):
        read.run_report(owner, "fifo_lots")
    with pytest.raises(Refused, match="Choose a report"):
        read.run_report(owner, "nonsense")


def test_fifo_report_lists_lots(books, owner):
    from mcpserver import read
    from purchasing.posting.bills import BillItemSpec

    create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-2",
                item_lines=[BillItemSpec(books.widget, 10, D("10.00"))], expense_lines=[])
    out = read.run_report(owner, "fifo_lots", sku="WID-1", start="2026-01-01", end="2026-12-31")
    assert out["received"] == 10 and out["on_hand"] == 10 and out["value"] == "100.00"
    assert out["lots"][0]["quantity"] == 10 and out["lots"][0]["unit_cost"] == "10.00"
    with pytest.raises(Refused, match="is not costed by FIFO"):
        read.run_report(owner, "fifo_lots", sku="SVC")

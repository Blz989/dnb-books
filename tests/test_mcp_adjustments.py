"""Claude tools: adjustments on bills and invoices, and edit_bill keeping left-out lines (adjustments spec, Task 7)."""
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ledger.models import AccountSubtype, AccountType
from mcpserver import read
from purchasing.models import Bill, PurchasingSettings
from purchasing.posting.bills import BillAdjustmentSpec, BillExpenseSpec, BillItemSpec, bill_adjustment, create_bill
from sales.models import Invoice
from sales.posting.invoices import InvoiceAdjustmentSpec, InvoiceLineSpec, create_invoice, invoice_adjustment
from tests.conftest import _account
from tests.test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (shared fixtures)
from tests.test_mcp_voids import DAY, confirm, preview

D = Decimal


@pytest.fixture
def wholesale(books):
    account = _account("5010", "Wholesale Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
    settings = PurchasingSettings.load()
    settings.default_adjustment_account = account
    settings.save()
    return account


def test_create_bill_with_adjustment_uses_the_default_account(books, owner, wholesale):
    p = preview(owner, "create_bill", {"vendor": "Acme Supply", "date": "2026-10-15",
                                       "expense_lines": [{"account": "6150", "amount": "100.00"}],
                                       "adjustment": {"amount": "-7.50", "note": "Nova rounding"}})
    pv = p["preview"]
    assert (pv["subtotal"], pv["total"]) == ("100.00", "92.50")
    assert pv["adjustment"] == {"amount": "-7.50", "note": "Nova rounding", "account": str(wholesale)}
    assert len(pv["expense_lines"]) == 1
    confirm(owner, p)
    assert bill_adjustment(Bill.objects.get()).amount == D("-7.50")


def test_create_bill_adjustment_needs_an_account_without_a_default(books, owner):
    with pytest.raises(ToolError, match="Choose the adjustment account."):
        preview(owner, "create_bill", {"vendor": "Acme Supply", "expense_lines": [{"account": "6150", "amount": "10.00"}],
                                       "adjustment": {"amount": "-1.00"}})


def test_get_bill_shows_adjustment_and_credits_apart(books, owner, wholesale):
    bill = create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1",
                       expense_lines=[BillExpenseSpec(books.freight, D("40.00"))],
                       adjustment=BillAdjustmentSpec(wholesale, D("2.00")))
    got = read.get_bill(owner, bill.number)
    assert (got["subtotal"], got["total"], got["adjustment"]["amount"]) == ("40.00", "42.00", "2.00")
    assert [l["amount"] for l in got["expense_lines"]] == ["40.00"] and got["credits"] == []


def test_edit_bill_keeps_left_out_lines_and_the_adjustment(books, owner, wholesale):
    bill = create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1",
                       item_lines=[BillItemSpec(books.widget, 3, D("10.00"))],
                       expense_lines=[BillExpenseSpec(books.freight, D("40.00"))],
                       adjustment=BillAdjustmentSpec(wholesale, D("-5.00")))
    # Only expense lines given: the item line stays, and so does the adjustment.
    p = preview(owner, "edit_bill", {"number": bill.number, "item_lines": None,
                                     "expense_lines": [{"account": "6150", "amount": "50.00"}]})
    assert p["preview"]["after"]["total"] == "75.00"
    confirm(owner, p, bill.number)
    bill.refresh_from_db()
    assert bill.item_lines.get().quantity == 3 and bill_adjustment(bill).amount == D("-5.00")
    # Only the adjustment given: every line stays.
    p = preview(owner, "edit_bill", {"number": bill.number, "item_lines": None, "expense_lines": None,
                                     "adjustment": {"amount": "-15.00"}})
    confirm(owner, p, bill.number)
    assert bill_adjustment(bill).amount == D("-15.00") and bill_adjustment(bill).account == wholesale
    assert bill.expense_lines.filter(is_adjustment=False).get().amount == D("50.00")
    # 0.00 removes the adjustment; [] still removes lines.
    p = preview(owner, "edit_bill", {"number": bill.number, "item_lines": [], "expense_lines": None,
                                     "adjustment": {"amount": "0.00"}})
    confirm(owner, p, bill.number)
    assert bill_adjustment(bill) is None and not bill.item_lines.exists()


def test_edit_bill_tool_passes_none_for_a_left_out_list():
    from mcpserver.tools import _dump_kept

    assert _dump_kept(None) is None and _dump_kept([]) == []


def test_create_invoice_with_adjustment(books, owner):
    p = preview(owner, "create_invoice", {"customer": "Acme Builders", "date": "2026-10-15",
                                          "lines": [{"sku": "SVC", "quantity": 2}],
                                          "adjustment": {"amount": "-0.50"}})
    pv = p["preview"]
    assert (pv["subtotal"], pv["total"], pv["adjustment"]["amount"]) == ("200.00", "199.50", "-0.50")
    assert [l["sku"] for l in pv["lines"]] == ["SVC"]
    confirm(owner, p)
    assert invoice_adjustment(Invoice.objects.get()).item.sku == "ADJ"


def test_edit_invoice_keeps_then_removes_the_adjustment(books, owner):
    inv = create_invoice(customer=books.customer, date=DAY, user=owner, lines=[InvoiceLineSpec(books.service, 2, D("100.00"))],
                         adjustment=InvoiceAdjustmentSpec(D("3.00"), "Rush"))
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [{"sku": "SVC", "quantity": 3}]})
    assert p["preview"]["after"]["total"] == "303.00"
    confirm(owner, p, inv.number)
    assert invoice_adjustment(inv).description == "Rush"
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [{"sku": "SVC", "quantity": 3}],
                                        "adjustment": {"amount": "0.00"}})
    confirm(owner, p, inv.number)
    assert invoice_adjustment(inv) is None
    assert read.get_invoice(owner, inv.number)["adjustment"] is None

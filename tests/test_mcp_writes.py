"""MCP write tools beyond invoices and bills: preview first, confirm posts exactly the preview."""
from datetime import date
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from banking.models import AccountKind, BankAccount, Transfer
from ledger.models import Account, AccountSubtype, AccountType, JournalEntry
from ledger.periods import close_period
from mcpserver import write
from mcpserver.runtime import run
from purchasing.models import VendorCredit
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.models import CreditNote, Deposit, Payment, Refund, SalesOrder
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment
from tests.test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (shared fixtures)

D = Decimal
DAY = date(2026, 10, 15)


def go(user, op, payload, model=None):
    """Preview (nothing saved), then confirm. Returns (preview, result)."""
    before = JournalEntry.objects.count()
    count = model.objects.count() if model is not None else None
    p = run(ctx_for(user), op, write.preview, operation=op, payload=payload)
    assert JournalEntry.objects.count() == before
    if model is not None:
        assert model.objects.count() == count
    done = run(ctx_for(user), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"])
    assert done["document"]
    return p, done


@pytest.fixture
def stocked(books, owner):
    """Ten widgets on hand."""
    from purchasing.posting.bills import BillItemSpec

    create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1",
                item_lines=[BillItemSpec(books.widget, 10, D("10.00"))], expense_lines=[])
    return books


def test_sales_order(books, owner):
    p, done = go(owner, "create_sales_order", {
        "customer": "Acme Builders", "date": "2026-10-05", "lines": [{"sku": "WID-1", "quantity": 4}]}, SalesOrder)
    assert p["preview"]["total"] == "100.00" and p["preview"]["posts"].startswith("Nothing posts")
    assert SalesOrder.objects.get().number == done["document"]


def test_sales_order_needs_a_line(books, owner):
    with pytest.raises(ToolError, match="An order needs at least one line"):
        run(ctx_for(owner), "create_sales_order", write.preview, operation="create_sales_order",
            payload={"customer": "Acme Builders", "lines": []})


def test_credit_note_applied_to_an_invoice(books, owner, sales_settings):
    inv = create_invoice(customer=books.customer, date=DAY, user=owner,
                         lines=[InvoiceLineSpec(books.service, 1, D("100.00"))])
    p, done = go(owner, "create_credit_note", {
        "customer": "Acme Builders", "date": "2026-10-16", "reason": "Damaged",
        "lines": [{"description": "Damaged goods", "amount": "30.00"}],
        "apply_to": [{"invoice": inv.number, "amount": "30.00"}]}, CreditNote)
    assert p["preview"]["total"] == "30.00" and p["preview"]["applied"][0]["balance_after"] == "70.00"
    assert p["preview"]["unapplied"] == "0.00"
    assert CreditNote.objects.get().number == done["document"]


def test_vendor_credit_allowance_and_return(stocked, owner):
    bill = create_bill(vendor=stocked.vendor, date=DAY, user=owner, vendor_invoice_number="V-9", item_lines=[],
                       expense_lines=[BillExpenseSpec(stocked.freight, D("50.00"))])
    p, done = go(owner, "create_vendor_credit", {
        "vendor": "Acme Supply", "kind": "allowance", "date": "2026-10-16", "reason": "Late",
        "lines": [{"account": "6150", "amount": "20.00"}], "apply_to": [{"bill": bill.number, "amount": "20.00"}]},
        VendorCredit)
    assert p["preview"]["total"] == "20.00" and p["preview"]["applied"][0]["balance_after"] == "30.00"
    p2, _ = go(owner, "create_vendor_credit", {
        "vendor": "Acme Supply", "kind": "return", "date": "2026-10-17", "reason": "Wrong size",
        "lines": [{"sku": "WID-1", "quantity": 2}]}, VendorCredit)
    assert p2["preview"]["kind"] == "Return to vendor"
    assert VendorCredit.objects.count() == 2


def test_vendor_credit_kind_is_checked(books, owner):
    with pytest.raises(ToolError, match="Choose a kind: return"):
        run(ctx_for(owner), "create_vendor_credit", write.preview, operation="create_vendor_credit",
            payload={"vendor": "Acme Supply", "kind": "gift", "lines": []})


def test_refund_from_a_payment_is_admin_only(books, owner, clerk, sales_settings):
    payment = receive_payment(customer=books.customer, date=DAY, method="CHECK", amount=D("80.00"), user=owner,
                              applications=[])
    payload = {"source": payment.number, "amount": "50.00", "method": "check", "bank_account": "1000",
               "date": "2026-10-16", "reference": ""}
    with pytest.raises(ToolError, match=r"Your role \(Standard\) can't do this. Ask an admin."):
        run(ctx_for(clerk), "issue_refund", write.preview, operation="issue_refund", payload=payload)
    p, done = go(owner, "issue_refund", payload, Refund)
    assert p["preview"]["left_on_source"] == "30.00"
    assert Refund.objects.get().number == done["document"]
    with pytest.raises(ToolError, match="No customer payment or credit note has the number PAY-NOPE"):
        run(ctx_for(owner), "issue_refund", write.preview, operation="issue_refund",
            payload={**payload, "source": "PAY-NOPE"})


def test_deposit(books, owner, sales_settings):
    payment = receive_payment(customer=books.customer, date=DAY, method="CHECK", amount=D("80.00"), user=owner,
                              applications=[])
    p, done = go(owner, "make_deposit", {"payments": [payment.number], "bank_account": "1000",
                                         "date": "2026-10-16"}, Deposit)
    assert p["preview"]["total"] == "80.00"
    assert Payment.objects.get(pk=payment.pk).deposit.number == done["document"]


def test_transfer_between_bank_accounts(books, owner):
    other = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    BankAccount.objects.create(ledger_account=books.bank, kind=AccountKind.CHECKING)
    BankAccount.objects.create(ledger_account=other, kind=AccountKind.SAVINGS)
    p, done = go(owner, "create_transfer", {"from_account": "1000", "to_account": "Savings", "amount": "500.00",
                                            "date": "2026-10-16"}, Transfer)
    assert p["preview"]["amount"] == "500.00" and done["document"] == f"TR-{Transfer.objects.get().pk}"
    with pytest.raises(ToolError, match="Choose two different accounts."):
        run(ctx_for(owner), "create_transfer", write.preview, operation="create_transfer",
            payload={"from_account": "1000", "to_account": "1000", "amount": "5.00"})
    with pytest.raises(ToolError, match="is not a bank account"):
        run(ctx_for(owner), "create_transfer", write.preview, operation="create_transfer",
            payload={"from_account": "1000", "to_account": "6150", "amount": "5.00"})


def test_stock_adjustment_and_negative_warning(stocked, owner, inv_settings):
    p, _ = go(owner, "adjust_stock", {"date": "2026-10-16", "lines": [
        {"sku": "WID-1", "quantity": -3, "reason": "Damaged", "note": "forklift"}]})
    assert p["preview"]["adjustments"][0]["quantity"] == -3 and p["warnings"] == []
    p2 = write.preview(owner, "adjust_stock", {"date": "2026-10-17", "lines": [
        {"sku": "WID-1", "quantity": -50, "reason": "Lost"}]})
    assert "has 7 on hand" in p2["warnings"][0]
    done = write.confirm(owner, p2["confirmation_id"])
    assert done["result"]["adjustments"][0]["quantity"] == -50
    with pytest.raises(ToolError, match="Line 1: choose a reason"):
        run(ctx_for(owner), "adjust_stock", write.preview, operation="adjust_stock",
            payload={"lines": [{"sku": "WID-1", "quantity": 1, "reason": "Fun"}]})


def test_value_adjustment_is_admin_only(stocked, owner, clerk, inv_settings):
    payload = {"sku": "WID-1", "amount": "-5.00", "reason": "Write-down", "date": "2026-10-16"}
    with pytest.raises(ToolError, match=r"Your role \(Standard\)"):
        run(ctx_for(clerk), "adjust_item_value", write.preview, operation="adjust_item_value", payload=payload)
    p, _ = go(owner, "adjust_item_value", payload)
    assert p["preview"]["change"] in ("-5.00", "5.00")


def test_closed_month_blocks_new_writes_with_the_app_message(books, owner):
    close_period(2026, 9, owner, override_reason="test")
    with pytest.raises(ToolError, match="September 2026 is closed. Choose a date in an open month"):
        run(ctx_for(owner), "create_credit_note", write.preview, operation="create_credit_note", payload={
            "customer": "Acme Builders", "date": "2026-09-15", "lines": [{"amount": "5.00"}]})


# ---- journal entries ----

def test_journal_entry_previews_then_posts_on_confirm(books, owner):
    payload = {"date": "2026-10-20", "memo": "Owner draw", "reference": "OD-1", "lines": [
        {"account": "6150", "debit": "75.00", "description": "Freight accrual"},
        {"account": "1000", "credit": "75.00"}]}
    p, done = go(owner, "create_journal_entry", payload, JournalEntry)
    assert p["preview"]["total"] == "75.00" and p["preview"]["lines"][0]["debit"] == "75.00"
    entry = JournalEntry.objects.get()
    assert done["document"] == entry.display_number and entry.status == "POSTED" and entry.created_by == owner
    assert entry.memo == "Owner draw"


@pytest.mark.parametrize("lines, message", [
    ([{"account": "6150", "debit": "75.00"}, {"account": "1000", "credit": "70.00"}], "balance|equal|debits"),
    ([{"account": "9999", "debit": "5.00"}, {"account": "1000", "credit": "5.00"}], "No account is 9999"),
])
def test_journal_entry_refusals(books, owner, lines, message):
    with pytest.raises(ToolError, match=message):
        run(ctx_for(owner), "create_journal_entry", write.preview, operation="create_journal_entry",
            payload={"date": "2026-10-20", "lines": lines})
    assert JournalEntry.objects.count() == 0


def test_journal_entry_refuses_inventory_accounts(books, owner, inventory_account):
    with pytest.raises(ToolError, match="Line 1: inventory accounts can only change through stock movements."):
        run(ctx_for(owner), "create_journal_entry", write.preview, operation="create_journal_entry", payload={
            "date": "2026-10-20", "lines": [{"account": inventory_account.number, "debit": "5.00"},
                                           {"account": "1000", "credit": "5.00"}]})


def test_journal_entry_closed_month(books, owner):
    close_period(2026, 9, owner, override_reason="test")
    with pytest.raises(ToolError, match="September 2026 is closed"):
        run(ctx_for(owner), "create_journal_entry", write.preview, operation="create_journal_entry", payload={
            "date": "2026-09-20", "lines": [{"account": "6150", "debit": "5.00"}, {"account": "1000", "credit": "5.00"}]})


def test_journal_entry_follows_the_app_permission(books, clerk, django_user_model):
    """Standard users may add journal entries in the app, so they may here. A user without the permission may not."""
    lines = [{"account": "6150", "debit": "5.00"}, {"account": "1000", "credit": "5.00"}]
    assert write.preview(clerk, "create_journal_entry", {"date": "2026-10-20", "lines": lines})["preview"]["total"] == "5.00"
    nobody = django_user_model.objects.create_user("nobody", password="pw", is_staff=True)
    with pytest.raises(ToolError, match=r"Your role \(Standard\) can't do this. Ask an admin."):
        run(ctx_for(nobody, email="nobody@example.com"), "create_journal_entry", write.preview,
            operation="create_journal_entry", payload={"date": "2026-10-20", "lines": lines})


# ---- drop ship: sales order, then its PO, then the vendor's bill ----

def _drop_ship_order(owner, vendor_text="Acme Supply"):
    p = write.preview(owner, "create_sales_order", {"customer": "Acme Builders", "date": "2026-10-05", "lines": [
        {"sku": "WID-1", "quantity": 3, "drop_ship_vendor": vendor_text}]})
    return write.confirm(owner, p["confirmation_id"])["document"]


def test_drop_ship_order_then_po_then_bill(books, owner):
    from purchasing.models import BillLineKind, PurchaseOrder

    so = _drop_ship_order(owner)
    p, done = go(owner, "create_drop_ship_pos", {"sales_orders": [so], "date": "2026-10-06"}, PurchaseOrder)
    (po_summary,) = p["preview"]["purchase_orders"]
    assert po_summary["vendor"] == "Acme Supply" and po_summary["total"] == "30.00"
    assert po_summary["lines"][0]["for"] == [{"sales_order": so, "customer": "Acme Builders", "quantity": 3}]
    po = PurchaseOrder.objects.get()
    assert done["document"] == po.number
    from mcpserver import read

    assert read.get_purchase_order(owner, po.number)["drop_ship_for"] == so
    # The vendor's invoice: create_bill against the PO, by SKU.
    bp, _ = go(owner, "create_bill", {"vendor": "Acme Supply", "vendor_invoice_number": "AS-77", "date": "2026-10-10",
                                      "purchase_order": po.number, "item_lines": [{"sku": "WID-1", "quantity": 3}]})
    assert bp["preview"]["item_lines"][0]["kind"] == dict(BillLineKind.choices)[BillLineKind.DROP_SHIP]
    assert bp["preview"]["total"] == "30.00"
    assert read.get_purchase_order(owner, po.number)["lines"][0]["remaining"] == 0


def test_drop_ship_po_vendor_text_can_be_mapped_and_repeat_is_refused(books, owner):
    from purchasing.models import PurchaseOrder

    so = _drop_ship_order(owner, vendor_text="Acme Sply")
    with pytest.raises(ToolError, match="No vendor named Acme Sply. Choose a vendor for these lines."):
        run(ctx_for(owner), "create_drop_ship_pos", write.preview, operation="create_drop_ship_pos",
            payload={"sales_orders": [so]})
    go(owner, "create_drop_ship_pos", {"sales_orders": [so], "vendors": {"Acme Sply": "Acme Supply"}}, PurchaseOrder)
    with pytest.raises(ToolError, match="Every drop-ship line on this order is already on a purchase order"):
        run(ctx_for(owner), "create_drop_ship_pos", write.preview, operation="create_drop_ship_pos",
            payload={"sales_orders": [so]})
    with pytest.raises(ToolError, match="Give at least one sales order number."):
        run(ctx_for(owner), "create_drop_ship_pos", write.preview, operation="create_drop_ship_pos",
            payload={"sales_orders": []})

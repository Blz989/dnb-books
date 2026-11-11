"""MCP voids and edits: Admin only, document number typed back to confirm, reversing entries, closed months."""
from datetime import date
from decimal import Decimal

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ledger.models import EntryStatus, JournalEntry
from ledger.periods import close_period
from mcpserver import write
from mcpserver.models import McpCall, PendingChange
from mcpserver.runtime import run
from purchasing.models import Bill, DocStatus as BillStatus
from items.models import StockMovement
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill
from sales.models import DocStatus, Fulfillment, Invoice, Payment
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment
from tests.test_mcp import books, clerk, ctx_for, owner  # noqa: F401  (shared fixtures)

D = Decimal
DAY = date(2026, 10, 15)


@pytest.fixture
def invoice(books, owner):
    return create_invoice(customer=books.customer, date=DAY, user=owner,
                          lines=[InvoiceLineSpec(books.service, 2, D("100.00"))])


def preview(user, op, payload):
    return run(ctx_for(user), op, write.preview, operation=op, payload=payload)


def confirm(user, p, document=""):
    return run(ctx_for(user), "confirm_change", write.confirm, confirmation_id=p["confirmation_id"],
               confirm_document=document)


def test_void_invoice_previews_then_voids_with_a_reversing_entry(books, owner, invoice):
    entries = JournalEntry.objects.count()
    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "Entered twice"})
    assert f"void invoice {invoice.number} for Acme Builders (200.00)" in p["preview"]["effect"]
    assert invoice.journal_entry.display_number in p["preview"]["effect"]
    assert f"confirm_document set to {invoice.number}" in p["note"]
    assert Invoice.objects.get().status == DocStatus.OPEN and JournalEntry.objects.count() == entries
    done = confirm(owner, p, invoice.number.lower())  # any letter case is fine
    invoice.refresh_from_db()
    assert invoice.status == DocStatus.VOID and invoice.void_reason == "Entered twice"
    reversal = invoice.journal_entry.reversals.get()
    assert done["result"]["reversing_entry"] == reversal.display_number and reversal.status == EntryStatus.POSTED
    assert reversal.created_by == owner and JournalEntry.objects.count() == entries + 1


def test_confirm_needs_the_document_number_and_a_wrong_one_keeps_the_preview_usable(books, owner, invoice):
    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "Duplicate"})
    for wrong in ("", "INV-000999"):
        with pytest.raises(ToolError, match=rf"^Type the document number \({invoice.number}\) to confirm this void.$"):
            confirm(owner, p, wrong)
    assert PendingChange.objects.get(pk=p["confirmation_id"]).used_at is None
    assert Invoice.objects.get().status == DocStatus.OPEN
    confirm(owner, p, invoice.number)
    assert Invoice.objects.get().status == DocStatus.VOID


def test_standard_user_cannot_preview_or_confirm_a_void(books, owner, clerk, invoice):
    with pytest.raises(ToolError, match=r"^Your role \(Standard\) can't do this. Ask an admin.$"):
        preview(clerk, "void_invoice", {"number": invoice.number, "reason": "x"})
    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "Duplicate"})
    with pytest.raises(ToolError, match="No preview has that confirmation_id"):
        confirm(clerk, p, invoice.number)  # another person's preview
    assert Invoice.objects.get().status == DocStatus.OPEN


def test_a_demoted_admin_cannot_confirm_an_old_preview(books, owner, invoice):
    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "Duplicate"})
    from django.contrib.auth.models import Group

    owner.groups.clear()
    owner.groups.add(Group.objects.get(name="Standard"))
    with pytest.raises(ToolError, match=r"Your role \(Standard\)"):
        confirm(owner, p, invoice.number)
    assert Invoice.objects.get().status == DocStatus.OPEN


def test_void_needs_a_reason_and_a_known_document(books, owner, invoice):
    with pytest.raises(ToolError, match="^Enter a reason for the void.$"):
        preview(owner, "void_invoice", {"number": invoice.number, "reason": "  "})
    with pytest.raises(ToolError, match="No customer invoice has the number|No invoice has the number INV-009999"):
        preview(owner, "void_invoice", {"number": "INV-009999", "reason": "x"})


def test_void_refused_with_the_apps_message_when_a_payment_is_applied(books, owner, invoice):
    receive_payment(customer=books.customer, date=DAY, method="CHECK", amount=D("200.00"), user=owner,
                    applications=[(invoice, D("200.00"))])
    with pytest.raises(ToolError, match="has payments or credits applied. Remove them first."):
        preview(owner, "void_invoice", {"number": invoice.number, "reason": "x"})


def test_void_payment_effect_names_what_it_unapplies(books, owner, invoice):
    payment = receive_payment(customer=books.customer, date=DAY, method="CHECK", amount=D("200.00"), user=owner,
                              applications=[(invoice, D("200.00"))])
    p = preview(owner, "void_customer_payment", {"number": payment.number, "reason": "Bounced"})
    assert f"It will unapply: {invoice.number} 200.00." in p["preview"]["effect"]
    confirm(owner, p, payment.number)
    assert Payment.objects.get().status == DocStatus.VOID


def test_void_bill_and_journal_entry(books, owner):
    bill = create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1", item_lines=[],
                       expense_lines=[BillExpenseSpec(books.freight, D("40.00"))])
    p = preview(owner, "void_bill", {"number": bill.number, "reason": "Wrong vendor"})
    confirm(owner, p, bill.number)
    assert Bill.objects.get().status == BillStatus.VOID
    je = write.confirm(owner, write.preview(owner, "create_journal_entry", {
        "date": "2026-10-20", "lines": [{"account": "6150", "debit": "9.00"}, {"account": "1000", "credit": "9.00"}]}
    )["confirmation_id"])["document"]
    p2 = preview(owner, "void_journal_entry", {"number": je, "reason": "Posted to the wrong account"})
    confirm(owner, p2, je)
    assert JournalEntry.objects.get(pk=int(je.split("-")[1])).status == EntryStatus.VOID


def test_void_in_a_closed_month_is_blocked_with_the_app_message(books, owner, invoice):
    close_period(2026, 10, owner, override_reason="test")
    with pytest.raises(ToolError, match="October 2026 is closed. Choose a date in an open month"):
        preview(owner, "void_invoice", {"number": invoice.number, "reason": "x"})


def test_a_month_closed_between_preview_and_confirm_blocks_the_void(books, owner, invoice):
    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "x"})
    close_period(2026, 10, owner, override_reason="test")
    with pytest.raises(ToolError, match="October 2026 is closed"):
        confirm(owner, p, invoice.number)
    assert Invoice.objects.get().status == DocStatus.OPEN


def test_void_is_audited_under_the_admin_and_logged(books, owner, invoice):
    from auditlog.models import LogEntry

    p = preview(owner, "void_invoice", {"number": invoice.number, "reason": "Duplicate"})
    confirm(owner, p, invoice.number)
    assert McpCall.objects.filter(tool="confirm_change", ok=True, document=invoice.number).exists()
    assert LogEntry.objects.filter(actor=owner, object_pk=str(invoice.pk)).exists()


# ---- edits ----

def test_edit_invoice_shows_before_and_after_and_reposts(books, owner, invoice):
    old_entry = invoice.journal_entry
    p = preview(owner, "edit_invoice", {"number": invoice.number, "lines": [{"sku": "SVC", "quantity": 3}],
                                        "reason": "Customer ordered more"})
    assert p["preview"]["before"]["total"] == "200.00" and p["preview"]["after"]["total"] == "300.00"
    assert Invoice.objects.get().lines.get().quantity == 2  # the preview changed nothing
    done = confirm(owner, p, invoice.number)
    invoice.refresh_from_db()
    assert invoice.lines.get().quantity == 3 and done["result"]["after"]["total"] == "300.00"
    old_entry.refresh_from_db()
    assert old_entry.status == EntryStatus.VOID and invoice.journal_entry_id != old_entry.pk


def test_edit_needs_admin_and_the_number(books, owner, clerk, invoice):
    payload = {"number": invoice.number, "lines": [{"sku": "SVC", "quantity": 3}]}
    with pytest.raises(ToolError, match=r"Your role \(Standard\)"):
        preview(clerk, "edit_invoice", payload)
    p = preview(owner, "edit_invoice", payload)
    with pytest.raises(ToolError, match=rf"^Type the document number \({invoice.number}\) to confirm this edit.$"):
        confirm(owner, p)


def test_edit_bill_keeps_unchanged_lines_and_replaces_expenses(books, owner):
    bill = create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-1", item_lines=[],
                       expense_lines=[BillExpenseSpec(books.freight, D("40.00"))])
    p = preview(owner, "edit_bill", {"number": bill.number, "expense_lines": [{"account": "6150", "amount": "55.00"}]})
    assert p["preview"]["before"]["total"] == "40.00" and p["preview"]["after"]["total"] == "55.00"
    confirm(owner, p, bill.number)
    assert Bill.objects.get().expense_lines.get().amount == D("55.00")


def test_edit_sales_order_and_purchase_order(books, owner):
    from purchasing.models import PurchaseOrder
    from sales.models import SalesOrder

    so = write.confirm(owner, write.preview(owner, "create_sales_order", {
        "customer": "Acme Builders", "date": "2026-10-05", "lines": [{"sku": "WID-1", "quantity": 4}]}
    )["confirmation_id"])["document"]
    p = preview(owner, "edit_sales_order", {"number": so, "lines": [{"sku": "WID-1", "quantity": 6}]})
    assert p["preview"]["after"]["total"] == "150.00"
    confirm(owner, p, so)
    assert SalesOrder.objects.get().lines.get().quantity == 6
    po = write.confirm(owner, write.preview(owner, "create_purchase_order", {
        "vendor": "Acme Supply", "date": "2026-10-05", "lines": [{"sku": "WID-1", "quantity": 5}]}
    )["confirmation_id"])["document"]
    p2 = preview(owner, "edit_purchase_order", {"number": po, "lines": [{"sku": "WID-1", "quantity": 8}]})
    confirm(owner, p2, po)
    assert PurchaseOrder.objects.get().lines.get().quantity == 8



# ---- edits keep drop-ship lines ----

def _drop_ship_invoice(books, owner):
    return create_invoice(customer=books.customer, date=DAY, user=owner, lines=[
        InvoiceLineSpec(books.widget, 2, D("25.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Supply"),
        InvoiceLineSpec(books.service, 1, D("100.00"))])


def test_edit_invoice_keeps_a_drop_ship_line_when_no_vendor_is_given(books, owner):
    inv = _drop_ship_invoice(books, owner)
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [
        {"sku": "WID-1", "quantity": 3, "drop_ship_vendor": None}, {"sku": "SVC", "quantity": 1}]})
    confirm(owner, p, inv.number)
    line = inv.lines.get(item=books.widget)
    assert (line.quantity, line.fulfillment, line.drop_ship_vendor) == (3, Fulfillment.DROP_SHIP, "Acme Supply")
    assert inv.lines.get(item=books.service).fulfillment == Fulfillment.STOCK


def test_edit_invoice_can_change_or_clear_the_drop_ship_vendor(books, owner):
    inv = _drop_ship_invoice(books, owner)
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [
        {"sku": "WID-1", "quantity": 2, "drop_ship_vendor": "Birch Wholesale"}, {"sku": "SVC", "quantity": 1}]})
    confirm(owner, p, inv.number)
    assert inv.lines.get(item=books.widget).drop_ship_vendor == "Birch Wholesale"
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [
        {"sku": "WID-1", "quantity": 2, "drop_ship_vendor": ""}, {"sku": "SVC", "quantity": 1}]})
    confirm(owner, p, inv.number)
    line = inv.lines.get(item=books.widget)
    assert (line.fulfillment, line.drop_ship_vendor) == (Fulfillment.STOCK, "")


def test_edit_invoice_new_line_is_stock_unless_a_vendor_is_given(books, owner):
    inv = _drop_ship_invoice(books, owner)
    lines = [{"sku": "WID-1", "quantity": 2}, {"sku": "SVC", "quantity": 1},
             {"sku": "WID-1", "quantity": 1, "drop_ship_vendor": ""}]
    confirm(owner, preview(owner, "edit_invoice", {"number": inv.number, "lines": lines}), inv.number)
    widgets = list(inv.lines.filter(item=books.widget).order_by("pk"))
    assert [w.fulfillment for w in widgets] == [Fulfillment.DROP_SHIP, Fulfillment.STOCK]


def test_edit_sales_order_keeps_a_drop_ship_line_when_no_vendor_is_given(books, owner):
    from sales.models import SalesOrder

    so = write.confirm(owner, write.preview(owner, "create_sales_order", {
        "customer": "Acme Builders", "date": "2026-10-05",
        "lines": [{"sku": "WID-1", "quantity": 4, "drop_ship_vendor": "Acme Supply"}]})["confirmation_id"])["document"]
    p = preview(owner, "edit_sales_order", {"number": so, "lines": [{"sku": "WID-1", "quantity": 6}]})
    confirm(owner, p, so)
    line = SalesOrder.objects.get().lines.get()
    assert (line.quantity, line.fulfillment, line.drop_ship_vendor) == (6, Fulfillment.DROP_SHIP, "Acme Supply")
    p = preview(owner, "edit_sales_order", {"number": so, "lines": [{"sku": "WID-1", "quantity": 6,
                                                                      "drop_ship_vendor": ""}]})
    confirm(owner, p, so)
    assert SalesOrder.objects.get().lines.get().fulfillment == Fulfillment.STOCK


def test_edit_of_a_document_with_no_journal_entry_previews(books, owner):
    inv = create_invoice(customer=books.customer, date=DAY, user=owner,
                         lines=[InvoiceLineSpec(books.service, 1, D("0.00"))])
    assert inv.journal_entry is None
    p = preview(owner, "edit_invoice", {"number": inv.number, "lines": [{"sku": "SVC", "quantity": 2, "price": "0.00"}]})
    assert p["preview"]["effect"] == "The invoice totals zero, so nothing posts to the ledger."
    confirm(owner, p, inv.number)
    assert inv.lines.get().quantity == 2


def test_edit_bill_with_no_journal_entry_previews(books, owner):
    bill = create_bill(vendor=books.vendor, date=DAY, user=owner, vendor_invoice_number="V-0", item_lines=[
        BillItemSpec(books.widget, 1, D("0.00"))], expense_lines=[])
    assert bill.journal_entry is None
    p = preview(owner, "edit_bill", {"number": bill.number, "item_lines": [{"sku": "WID-1", "quantity": 2, "price": "0.00"}]})
    confirm(owner, p, bill.number)
    assert Bill.objects.get().item_lines.get().quantity == 2


# ---- a document's own entry and movement can't be voided directly ----

def test_void_journal_entry_of_a_document_is_refused_with_the_lock_message(books, owner, invoice):
    number = invoice.journal_entry.display_number
    with pytest.raises(ToolError, match=f"^This entry belongs to invoice {invoice.number}. Void the invoice instead.$"):
        preview(owner, "void_journal_entry", {"number": number, "reason": "x"})


def test_void_stock_movement_of_a_document_is_refused_with_the_lock_message(books, owner):
    inv = create_invoice(customer=books.customer, date=DAY, user=owner,
                         lines=[InvoiceLineSpec(books.widget, 1, D("25.00"))],
                         allow_negative=True)
    movement = StockMovement.objects.get(item=books.widget)
    with pytest.raises(ToolError, match=f"^This movement belongs to invoice {inv.number}. Void or edit the invoice instead.$"):
        preview(owner, "void_stock_movement", {"number": f"SM-{movement.pk}", "reason": "x"})

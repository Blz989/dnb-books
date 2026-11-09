"""The tool list Claude sees. Each tool is a thin typed wrapper; the work is in read.py and write.py."""
from typing import Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import read, write
from .runtime import run

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PREVIEW = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CONFIRM = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


class SaleLine(BaseModel):
    sku: str
    quantity: int
    price: str | None = Field(None, description="Unit price like 12.50. Leave out to use the item's sale price.")
    description: str = ""
    drop_ship_vendor: str = Field("", description="Vendor name when the line ships direct from the vendor.")


class PurchaseLine(BaseModel):
    sku: str
    quantity: int
    price: str | None = Field(None, description="Unit cost like 4.25. Leave out to use the PO price or item cost.")
    description: str = ""


class ExpenseLine(BaseModel):
    account: str = Field(description="Expense account number or name, like 6100 or Freight.")
    amount: str
    description: str = ""


class InvoicePayment(BaseModel):
    invoice: str = Field(description="Invoice number, like INV-000123.")
    amount: str


class BillPayment(BaseModel):
    bill: str = Field(description="Bill number.")
    amount: str | None = Field(None, description="Leave out to pay the full balance.")


def _dump(lines):
    return [l.model_dump() for l in lines or []]


def register(mcp):
    # ---- read ----

    @mcp.tool(annotations=READ)
    def home_summary(ctx: Context) -> dict:
        """The home page figures: open and overdue receivables, bills due, cash, stock value, items to reorder."""
        return run(ctx, "home_summary", read.home_summary)

    @mcp.tool(annotations=READ)
    def search_customers(ctx: Context, query: str = "", include_inactive: bool = False) -> dict:
        """Customers whose name, contact, or email contains the query, with balances. Empty query lists all."""
        return run(ctx, "search_customers", read.search_customers, query=query, include_inactive=include_inactive)

    @mcp.tool(annotations=READ)
    def get_customer(ctx: Context, name: str) -> dict:
        """One customer by exact name: contact details, terms, balance, open invoices, recent payments."""
        return run(ctx, "get_customer", read.get_customer, name=name)

    @mcp.tool(annotations=READ)
    def search_vendors(ctx: Context, query: str = "", include_inactive: bool = False) -> dict:
        """Vendors whose name, contact, or email contains the query, with balances."""
        return run(ctx, "search_vendors", read.search_vendors, query=query, include_inactive=include_inactive)

    @mcp.tool(annotations=READ)
    def get_vendor(ctx: Context, name: str) -> dict:
        """One vendor by exact name: details, balance, open bills, recent payments. Tax IDs are masked."""
        return run(ctx, "get_vendor", read.get_vendor, name=name)

    @mcp.tool(annotations=READ)
    def search_items(ctx: Context, query: str = "", include_inactive: bool = False) -> dict:
        """Items whose SKU, name, or vendor part number contains the query, with on hand and on order."""
        return run(ctx, "search_items", read.search_items, query=query, include_inactive=include_inactive)

    @mcp.tool(annotations=READ)
    def get_item(ctx: Context, sku: str) -> dict:
        """One item by SKU: on hand, on order, reorder point, cost, prices, stock value."""
        return run(ctx, "get_item", read.get_item, sku=sku)

    @mcp.tool(annotations=READ)
    def list_invoices(ctx: Context, customer: str = "", status: Literal["open", "overdue", "all", "void"] = "open",
                      start: str = "", end: str = "") -> dict:
        """Invoices, newest first, filtered by customer name, status, and invoice date range (YYYY-MM-DD)."""
        return run(ctx, "list_invoices", read.list_invoices, customer=customer, status=status, start=start, end=end)

    @mcp.tool(annotations=READ)
    def get_invoice(ctx: Context, number: str) -> dict:
        """One invoice: lines, payments and credits applied, balance."""
        return run(ctx, "get_invoice", read.get_invoice, number=number)

    @mcp.tool(annotations=READ)
    def list_bills(ctx: Context, vendor: str = "", status: Literal["open", "overdue", "all", "void"] = "open",
                   due_through: str = "") -> dict:
        """Bills, soonest due first, filtered by vendor name, status, and due on or before a date."""
        return run(ctx, "list_bills", read.list_bills, vendor=vendor, status=status, due_through=due_through)

    @mcp.tool(annotations=READ)
    def get_bill(ctx: Context, number: str) -> dict:
        """One bill: item and expense lines, payments applied, balance."""
        return run(ctx, "get_bill", read.get_bill, number=number)

    @mcp.tool(annotations=READ)
    def list_purchase_orders(ctx: Context, vendor: str = "", status: Literal["open", "closed", "all"] = "open") -> dict:
        """Purchase orders, newest first."""
        return run(ctx, "list_purchase_orders", read.list_purchase_orders, vendor=vendor, status=status)

    @mcp.tool(annotations=READ)
    def get_purchase_order(ctx: Context, number: str) -> dict:
        """One purchase order: lines with billed and remaining quantities, and its bills."""
        return run(ctx, "get_purchase_order", read.get_purchase_order, number=number)

    @mcp.tool(annotations=READ)
    def search_journal_entries(ctx: Context, query: str = "", start: str = "", end: str = "",
                               status: Literal["", "draft", "posted", "void"] = "") -> dict:
        """Journal entries matching a memo, reference, document number, or JE number, with their lines."""
        return run(ctx, "search_journal_entries", read.search_journal_entries, query=query, start=start, end=end,
                   status=status)

    @mcp.tool(annotations=READ)
    def run_report(
        ctx: Context,
        report: Literal["trial_balance", "profit_and_loss", "balance_sheet", "ar_aging", "ap_aging", "open_invoices",
                        "bills_due", "low_stock", "inventory_valuation", "order_profitability", "cash_forecast"],
        as_of: str = "", start: str = "", end: str = "", basis: Literal["accrual", "cash"] = "accrual",
        compare_years: int = Field(0, ge=0, le=3, description="Same dates in up to 3 earlier years."),
        view: Literal["daily", "weekly", "monthly"] = "weekly", overdue_only: bool = False,
    ) -> dict:
        """Run a report. as_of (YYYY-MM-DD) is for balance sheet, trial balance, agings, open invoices, valuation,
        and the due-through date of bills_due. start and end are for profit and loss and order profitability.
        basis and compare_years apply to profit and loss; compare_years also to balance sheet and trial balance.
        view applies to cash_forecast. Dates default to today, and start to January 1."""
        return run(ctx, "run_report", read.run_report, report=report, as_of=as_of, start=start, end=end, basis=basis,
                   compare_years=compare_years, view=view, overdue_only=overdue_only)

    # ---- write: preview ----

    @mcp.tool(annotations=PREVIEW)
    def create_invoice(ctx: Context, customer: str, lines: list[SaleLine], date: str = "", sales_order: str = "",
                       terms: str = "", ship_to: str = "", notes: str | None = None) -> dict:
        """Preview a new invoice. With sales_order, lines invoice that order's open lines by SKU and quantity.
        Returns a preview and a confirmation_id; nothing is saved until confirm_change."""
        payload = {"customer": customer, "lines": _dump(lines), "date": date, "sales_order": sales_order,
                   "terms": terms, "ship_to": ship_to, "notes": notes}
        return run(ctx, "create_invoice", write.preview, operation="create_invoice", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def receive_payment(ctx: Context, customer: str, amount: str, method: Literal["check", "cash", "ach", "wire"],
                        date: str = "", bank_account: str = "", reference: str = "",
                        apply_to: list[InvoicePayment] | None = None) -> dict:
        """Preview a customer payment. ACH and wire need the bank account it went to; checks and cash go to
        undeposited funds. Without apply_to, it pays the oldest invoices first. Nothing is saved until confirm_change.
        The preview includes an upload link for the bill or receipt; pass it on to the person."""
        payload = {"customer": customer, "amount": amount, "method": method, "date": date,
                   "bank_account": bank_account, "reference": reference, "apply_to": _dump(apply_to)}
        return run(ctx, "receive_payment", write.preview, operation="receive_payment", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_bill(ctx: Context, vendor: str, vendor_invoice_number: str = "", date: str = "",
                    item_lines: list[PurchaseLine] | None = None, expense_lines: list[ExpenseLine] | None = None,
                    purchase_order: str = "", terms: str = "", notes: str = "") -> dict:
        """Preview a new bill with item lines, expense lines, or both. With purchase_order, item lines receive
        that PO's open lines by SKU. A duplicate vendor invoice number shows as a warning. Nothing is saved until
        confirm_change. The preview includes an upload link for the bill or receipt; pass it on to the person."""
        payload = {"vendor": vendor, "vendor_invoice_number": vendor_invoice_number, "date": date,
                   "item_lines": _dump(item_lines), "expense_lines": _dump(expense_lines),
                   "purchase_order": purchase_order, "terms": terms, "notes": notes}
        return run(ctx, "create_bill", write.preview, operation="create_bill", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def pay_bills(ctx: Context, vendor: str, method: Literal["check", "ach", "card"], paid_from: str,
                  bills: list[BillPayment] | None = None, amount: str = "", date: str = "", check_number: str = "",
                  reference: str = "") -> dict:
        """Preview a vendor payment. paid_from is the bank or card account number or name. amount defaults to the
        bills paid. A check without a number goes to Checks to print. Nothing is saved until confirm_change.
        The preview includes an upload link for the bill or receipt; pass it on to the person."""
        payload = {"vendor": vendor, "method": method, "paid_from": paid_from, "bills": _dump(bills),
                   "amount": amount, "date": date, "check_number": check_number, "reference": reference}
        return run(ctx, "pay_bills", write.preview, operation="pay_bills", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_purchase_order(ctx: Context, vendor: str, lines: list[PurchaseLine], date: str = "",
                              expected_date: str = "", notes: str = "") -> dict:
        """Preview a new purchase order. Nothing is saved until confirm_change."""
        payload = {"vendor": vendor, "lines": _dump(lines), "date": date, "expected_date": expected_date, "notes": notes}
        return run(ctx, "create_purchase_order", write.preview, operation="create_purchase_order", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def save_customer(ctx: Context, name: str, fields: dict[str, str | bool] | None = None) -> dict:
        """Preview creating a customer, or updating the one with this exact name. fields can set: name (to rename),
        contact_name, email, phone, billing_address, ship_to_address, terms, resale_certificate, notes, is_active.
        Nothing is saved until confirm_change."""
        return run(ctx, "save_customer", write.preview, operation="save_customer", payload={"name": name, "fields": fields or {}})

    @mcp.tool(annotations=PREVIEW)
    def save_vendor(ctx: Context, name: str, fields: dict[str, str | bool] | None = None) -> dict:
        """Preview creating a vendor, or updating the one with this exact name. fields can set: name, contact_name,
        email, phone, address, terms, default_expense_account (number or name), is_1099, tax_id, notes, is_active.
        Tax IDs are never shown back. Nothing is saved until confirm_change."""
        return run(ctx, "save_vendor", write.preview, operation="save_vendor", payload={"name": name, "fields": fields or {}})

    @mcp.tool(annotations=PREVIEW)
    def save_item(ctx: Context, sku: str = "", fields: dict[str, str | bool | int | None] | None = None) -> dict:
        """Preview creating an item, or updating the one with this SKU. A new item without a SKU gets the next SKU in
        the series. fields can set: sku, name, description, item_type (STOCK or NON_STOCK), category, unit,
        packaging, sale_price, purchase_cost, vendor_part_number, costing_method, income_account, cogs_account,
        inventory_account (account numbers), reorder_point, is_active. Nothing is saved until confirm_change."""
        return run(ctx, "save_item", write.preview, operation="save_item", payload={"sku": sku, "fields": fields or {}})

    @mcp.tool(annotations=PREVIEW)
    def attach_file(ctx: Context, document: str) -> dict:
        """Get a one-time link for attaching a PDF or photo to an existing bill or payment. The person opens the
        link to upload; nothing else changes. document is a bill or payment number, like BILL-00045."""
        return run(ctx, "attach_file", write.attach_file, document=document)

    # ---- write: confirm ----

    @mcp.tool(annotations=CONFIRM)
    def confirm_change(ctx: Context, confirmation_id: str) -> dict:
        """Make a previewed change. Call only after the person has seen the preview and agreed. Each
        confirmation_id works once, within 15 minutes, for the person who asked for the preview."""
        return run(ctx, "confirm_change", write.confirm, confirmation_id=confirmation_id)

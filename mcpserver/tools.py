"""The tool list Claude sees. Each tool is a thin typed wrapper; the work is in read.py and write.py."""
from typing import Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import read, read_more, write, write_void
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


class CreditLine(BaseModel):
    description: str = ""
    amount: str
    account: str = Field("", description="Account number or name. Leave out to use the default returns account.")


class VendorCreditLine(BaseModel):
    sku: str = Field("", description="For a return: the SKU going back to the vendor.")
    quantity: int = Field(0, description="For a return: the quantity.")
    amount: str | None = Field(None, description="Return: leave out for quantity times cost. Allowance: required.")
    account: str = Field("", description="For an allowance: the account credited. Defaults to the vendor's.")
    description: str = ""


class StockAdjustLine(BaseModel):
    sku: str
    quantity: int = Field(description="Positive adds stock, negative removes it.")
    reason: Literal["Damaged", "Lost", "Sample", "Correction", "Other"]
    note: str = ""
    account: str = Field("", description="Expense account number or name. Leave out for the default.")


class EntryLine(BaseModel):
    account: str = Field(description="Account number or name, like 6100 or Freight.")
    debit: str = ""
    credit: str = ""
    description: str = ""


class BillCredit(BaseModel):
    bill: str
    amount: str | None = Field(None, description="Leave out to apply up to the bill's balance.")


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
    def list_sales_orders(ctx: Context, customer: str = "", status: Literal["open", "closed", "all"] = "open") -> dict:
        """Sales orders, newest first, filtered by customer name and status."""
        return run(ctx, "list_sales_orders", read_more.list_sales_orders, customer=customer, status=status)

    @mcp.tool(annotations=READ)
    def get_sales_order(ctx: Context, number: str) -> dict:
        """One sales order: lines with remaining quantities to invoice, and its invoices."""
        return run(ctx, "get_sales_order", read_more.get_sales_order, number=number)

    @mcp.tool(annotations=READ)
    def list_customer_payments(ctx: Context, customer: str = "", start: str = "", end: str = "") -> dict:
        """Customer payments received, newest first, with the amount not yet applied to invoices."""
        return run(ctx, "list_customer_payments", read_more.list_customer_payments, customer=customer, start=start,
                   end=end)

    @mcp.tool(annotations=READ)
    def get_customer_payment(ctx: Context, number: str) -> dict:
        """One customer payment: method, where it was deposited, and the invoices it was applied to."""
        return run(ctx, "get_customer_payment", read_more.get_customer_payment, number=number)

    @mcp.tool(annotations=READ)
    def list_vendor_payments(ctx: Context, vendor: str = "", start: str = "", end: str = "") -> dict:
        """Payments made to vendors, newest first, with check numbers."""
        return run(ctx, "list_vendor_payments", read_more.list_vendor_payments, vendor=vendor, start=start, end=end)

    @mcp.tool(annotations=READ)
    def get_vendor_payment(ctx: Context, number: str) -> dict:
        """One vendor payment: method, paid from, and the bills it was applied to."""
        return run(ctx, "get_vendor_payment", read_more.get_vendor_payment, number=number)

    @mcp.tool(annotations=READ)
    def list_credits(ctx: Context, kind: Literal["customer", "vendor"] = "customer",
                     status: Literal["open", "void", "all"] = "open") -> dict:
        """Customer credit notes or vendor credits. Open means some amount is still unapplied."""
        return run(ctx, "list_credits", read_more.list_credits, kind=kind, status=status)

    @mcp.tool(annotations=READ)
    def list_bank_accounts(ctx: Context, include_inactive: bool = False) -> dict:
        """Bank, card, and loan accounts with the balance in the books and the balance the bank last reported."""
        return run(ctx, "list_bank_accounts", read_more.list_bank_accounts, include_inactive=include_inactive)

    @mcp.tool(annotations=READ)
    def get_bank_account(ctx: Context, account: str) -> dict:
        """One bank account by number or name: balances and the last reconciled statement date."""
        return run(ctx, "get_bank_account", read_more.get_bank_account, account=account)

    @mcp.tool(annotations=READ)
    def list_stock_movements(ctx: Context, sku: str = "", start: str = "", end: str = "") -> dict:
        """Stock movements in a date range (default: this month), optionally for one SKU."""
        return run(ctx, "list_stock_movements", read_more.list_stock_movements, sku=sku, start=start, end=end)

    @mcp.tool(annotations=READ)
    def get_account_ledger(ctx: Context, account: str, start: str = "", end: str = "") -> dict:
        """The general ledger for one account (number or name): opening balance, lines, closing balance."""
        return run(ctx, "get_account_ledger", read_more.get_account_ledger, account=account, start=start, end=end)

    @mcp.tool(annotations=READ)
    def close_checklist(ctx: Context, month: str = "") -> dict:
        """What blocks closing a month (YYYY-MM, default last month), and whether it is closed. Read only:
        closing and reopening are done in the app."""
        return run(ctx, "close_checklist", read_more.close_checklist, month=month)

    @mcp.tool(annotations=READ)
    def run_report(
        ctx: Context,
        report: Literal["trial_balance", "profit_and_loss", "balance_sheet", "ar_aging", "ap_aging", "open_invoices",
                        "bills_due", "low_stock", "inventory_valuation", "order_profitability", "cash_forecast",
                        "general_ledger", "bank_register", "stock_movements", "fifo_lots", "negative_stock",
                        "customer_balances", "vendor_balances", "open_sales_orders", "open_purchase_orders",
                        "undeposited_funds", "drop_ship_awaiting_bill", "unpaid_invoices", "1099_summary"],
        as_of: str = "", start: str = "", end: str = "", basis: Literal["accrual", "cash"] = "accrual",
        compare_years: int = Field(0, ge=0, le=3, description="Same dates in up to 3 earlier years."),
        view: Literal["daily", "weekly", "monthly"] = "weekly", overdue_only: bool = False,
        account: str = Field("", description="Account number or name, for general_ledger and bank_register."),
        sku: str = Field("", description="Item SKU, for fifo_lots and optionally stock_movements."),
    ) -> dict:
        """Run a report. as_of (YYYY-MM-DD) is for balance sheet, trial balance, agings, open invoices, valuation,
        and the due-through date of bills_due. start and end are for profit and loss and order profitability.
        basis and compare_years apply to profit and loss; compare_years also to balance sheet and trial balance.
        view applies to cash_forecast. general_ledger and bank_register need account and use start and end.
        fifo_lots needs sku. 1099_summary uses the year of end. customer_balances, vendor_balances, and
        unpaid_invoices use as_of. Dates default to today, and start to January 1."""
        return run(ctx, "run_report", read.run_report, report=report, as_of=as_of, start=start, end=end, basis=basis,
                   compare_years=compare_years, view=view, overdue_only=overdue_only, account=account, sku=sku)

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
        undeposited funds. Without apply_to, it pays the oldest invoices first. Nothing is saved until confirm_change."""
        payload = {"customer": customer, "amount": amount, "method": method, "date": date,
                   "bank_account": bank_account, "reference": reference, "apply_to": _dump(apply_to)}
        return run(ctx, "receive_payment", write.preview, operation="receive_payment", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_bill(ctx: Context, vendor: str, vendor_invoice_number: str = "", date: str = "",
                    item_lines: list[PurchaseLine] | None = None, expense_lines: list[ExpenseLine] | None = None,
                    purchase_order: str = "", terms: str = "", notes: str = "") -> dict:
        """Preview a new bill with item lines, expense lines, or both. With purchase_order, item lines receive
        that PO's open lines by SKU. A duplicate vendor invoice number shows as a warning. Nothing is saved until
        confirm_change."""
        payload = {"vendor": vendor, "vendor_invoice_number": vendor_invoice_number, "date": date,
                   "item_lines": _dump(item_lines), "expense_lines": _dump(expense_lines),
                   "purchase_order": purchase_order, "terms": terms, "notes": notes}
        return run(ctx, "create_bill", write.preview, operation="create_bill", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def pay_bills(ctx: Context, vendor: str, method: Literal["check", "ach", "card"], paid_from: str,
                  bills: list[BillPayment] | None = None, amount: str = "", date: str = "", check_number: str = "",
                  reference: str = "") -> dict:
        """Preview a vendor payment. paid_from is the bank or card account number or name. amount defaults to the
        bills paid. A check without a number goes to Checks to print. Nothing is saved until confirm_change."""
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
    def create_sales_order(ctx: Context, customer: str, lines: list[SaleLine], date: str = "", ship_to: str = "",
                           notes: str = "") -> dict:
        """Preview a new sales order. Nothing posts to the ledger until it is invoiced. Nothing is saved until
        confirm_change."""
        payload = {"customer": customer, "lines": _dump(lines), "date": date, "ship_to": ship_to or None, "notes": notes}
        return run(ctx, "create_sales_order", write.preview, operation="create_sales_order", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_drop_ship_pos(ctx: Context, sales_orders: list[str], vendors: dict[str, str] | None = None,
                             date: str = "") -> dict:
        """Preview purchase orders for the drop-ship lines of these sales orders (numbers like SO-00012) that are not
        yet on a PO: one PO per vendor, each line linked to its sales orders, as the Drop-ship screen does. A line's
        vendor text must match a vendor; if one doesn't, vendors maps the text as typed to a real vendor name. Bill
        the PO later with create_bill and purchase_order. Nothing is saved until confirm_change."""
        payload = {"sales_orders": sales_orders, "vendors": vendors or {}, "date": date}
        return run(ctx, "create_drop_ship_pos", write.preview, operation="create_drop_ship_pos", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_credit_note(ctx: Context, customer: str, lines: list[CreditLine], reason: str = "", date: str = "",
                           apply_to: list[InvoicePayment] | None = None) -> dict:
        """Preview a customer credit note, optionally applied to invoices. Nothing is saved until confirm_change."""
        payload = {"customer": customer, "lines": _dump(lines), "reason": reason, "date": date,
                   "apply_to": _dump(apply_to)}
        return run(ctx, "create_credit_note", write.preview, operation="create_credit_note", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_vendor_credit(ctx: Context, vendor: str, kind: Literal["return", "allowance"],
                             lines: list[VendorCreditLine], reason: str = "", date: str = "",
                             apply_to: list[BillCredit] | None = None) -> dict:
        """Preview a vendor credit. A return sends stock back (SKU, quantity); an allowance is a price credit
        (account, amount). It can be applied to bills. Nothing is saved until confirm_change."""
        payload = {"vendor": vendor, "kind": kind, "lines": _dump(lines), "reason": reason, "date": date,
                   "apply_to": _dump(apply_to)}
        return run(ctx, "create_vendor_credit", write.preview, operation="create_vendor_credit", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def issue_refund(ctx: Context, source: str, amount: str, method: Literal["check", "ach"], bank_account: str,
                     date: str = "", reference: str = "") -> dict:
        """Preview refunding a customer from a payment or credit note (its number). Admin only, as in the app.
        Nothing is saved until confirm_change."""
        payload = {"source": source, "amount": amount, "method": method, "bank_account": bank_account, "date": date,
                   "reference": reference}
        return run(ctx, "issue_refund", write.preview, operation="issue_refund", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def make_deposit(ctx: Context, payments: list[str], bank_account: str, date: str = "") -> dict:
        """Preview depositing customer payments from undeposited funds into a bank account. payments are payment
        numbers. Nothing is saved until confirm_change."""
        return run(ctx, "make_deposit", write.preview, operation="make_deposit",
                   payload={"payments": payments, "bank_account": bank_account, "date": date})

    @mcp.tool(annotations=PREVIEW)
    def create_transfer(ctx: Context, from_account: str, to_account: str, amount: str,
                        kind: Literal["transfer", "card payoff", "loan payment"] = "transfer", date: str = "",
                        principal: str = "", interest: str = "", interest_account: str = "") -> dict:
        """Preview a transfer between two bank accounts. A card payoff goes to a credit card account. A loan payment
        goes to a loan account and needs principal and interest adding up to the amount, and the interest account.
        Nothing is saved until confirm_change."""
        payload = {"from_account": from_account, "to_account": to_account, "amount": amount, "kind": kind,
                   "date": date, "principal": principal, "interest": interest, "interest_account": interest_account}
        return run(ctx, "create_transfer", write.preview, operation="create_transfer", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def adjust_stock(ctx: Context, lines: list[StockAdjustLine], date: str = "") -> dict:
        """Preview a stock quantity adjustment (damaged, lost, sample, correction). Going below zero shows as a
        warning. Nothing is saved until confirm_change."""
        return run(ctx, "adjust_stock", write.preview, operation="adjust_stock",
                   payload={"lines": _dump(lines), "date": date})

    @mcp.tool(annotations=PREVIEW)
    def adjust_item_value(ctx: Context, sku: str, amount: str, reason: str, date: str = "", account: str = "") -> dict:
        """Preview a value adjustment to an item's stock (a write-up or write-down, signed). Admin only, as in the
        app. Nothing is saved until confirm_change."""
        payload = {"sku": sku, "amount": amount, "reason": reason, "date": date, "account": account}
        return run(ctx, "adjust_item_value", write.preview, operation="adjust_item_value", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def create_journal_entry(ctx: Context, lines: list[EntryLine], memo: str = "", reference: str = "",
                             date: str = "") -> dict:
        """Preview a manual journal entry. Debits must equal credits. Inventory accounts can't be used (use stock
        adjustments). Once confirmed it posts at once; fix a mistake by voiding it in the app. Nothing is saved until
        confirm_change."""
        payload = {"lines": _dump(lines), "memo": memo, "reference": reference, "date": date}
        return run(ctx, "create_journal_entry", write.preview, operation="create_journal_entry", payload=payload)

    # ---- write: voids and edits (Admin only; confirming needs the document number) ----

    def _register_void(tool: str, label: str, example: str):
        def void(ctx: Context, number: str, reason: str, date: str = "") -> dict:
            payload = {"number": number, "reason": reason, "date": date}
            return run(ctx, tool, write.preview, operation=tool, payload=payload)

        mcp.tool(name=tool, annotations=PREVIEW, description=(
            f"Preview voiding a {label}{example}. Admin only. A void posts a reversing entry and "
            f"never deletes. A reason is required. date is the void date (default: the document's date). Nothing "
            f"changes until confirm_change with the confirmation_id and confirm_document set to the number."))(void)

    for _tool, _label, _example in (
        ("void_invoice", "customer invoice", " (like INV-000123)"), ("void_customer_payment", "customer payment", ""),
        ("void_bill", "vendor bill", " (like BILL-00045)"), ("void_vendor_payment", "vendor payment", ""),
        ("void_credit_note", "customer credit note", ""), ("void_vendor_credit", "vendor credit", ""),
        ("void_refund", "customer refund", ""), ("void_deposit", "bank deposit", ""),
        ("void_transfer", "bank transfer (like TR-12)", ""), ("void_journal_entry", "journal entry (like JE-000456)", ""),
        ("void_stock_movement", "stock movement (SM- id from list_stock_movements)", ""),
    ):
        _register_void(_tool, _label, _example)

    @mcp.tool(annotations=PREVIEW)
    def edit_invoice(ctx: Context, number: str, lines: list[SaleLine], date: str = "", terms: str = "",
                     ship_to: str | None = None, notes: str | None = None, reason: str = "") -> dict:
        """Preview editing an invoice. Admin only. lines replace all current lines (a line with the same SKU keeps
        its link to the sales order and its old price unless you give one). The old posting is voided and a new one
        posted. Shows before and after. Nothing changes until confirm_change with confirm_document set to the number."""
        payload = {"number": number, "lines": _dump(lines), "date": date, "terms": terms, "ship_to": ship_to,
                   "notes": notes, "reason": reason}
        return run(ctx, "edit_invoice", write.preview, operation="edit_invoice", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def edit_bill(ctx: Context, number: str, item_lines: list[PurchaseLine] | None = None,
                  expense_lines: list[ExpenseLine] | None = None, vendor_invoice_number: str | None = None,
                  date: str = "", terms: str = "", notes: str | None = None, reason: str = "") -> dict:
        """Preview editing a bill. Admin only. item_lines and expense_lines replace all current lines (an item line
        with the same SKU keeps its PO link and old price unless you give one). Shows before and after. Nothing
        changes until confirm_change with confirm_document set to the number."""
        payload = {"number": number, "item_lines": _dump(item_lines), "expense_lines": _dump(expense_lines),
                   "vendor_invoice_number": vendor_invoice_number, "date": date, "terms": terms, "notes": notes,
                   "reason": reason}
        return run(ctx, "edit_bill", write.preview, operation="edit_bill", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def edit_sales_order(ctx: Context, number: str, lines: list[SaleLine], date: str = "", ship_to: str | None = None,
                         notes: str | None = None) -> dict:
        """Preview editing a sales order. Admin only. lines replace all current lines; lines already invoiced can't
        be removed. Nothing changes until confirm_change with confirm_document set to the number."""
        payload = {"number": number, "lines": _dump(lines), "date": date, "ship_to": ship_to, "notes": notes}
        return run(ctx, "edit_sales_order", write.preview, operation="edit_sales_order", payload=payload)

    @mcp.tool(annotations=PREVIEW)
    def edit_purchase_order(ctx: Context, number: str, lines: list[PurchaseLine], date: str = "",
                            expected_date: str = "", notes: str | None = None) -> dict:
        """Preview editing a purchase order. Admin only. lines replace all current lines. Drop-ship orders are edited
        in the app. Nothing changes until confirm_change with confirm_document set to the number."""
        payload = {"number": number, "lines": _dump(lines), "date": date, "expected_date": expected_date, "notes": notes}
        return run(ctx, "edit_purchase_order", write.preview, operation="edit_purchase_order", payload=payload)

    # ---- write: confirm ----

    @mcp.tool(annotations=CONFIRM)
    def confirm_change(ctx: Context, confirmation_id: str, confirm_document: str = "") -> dict:
        """Make a previewed change. Call only after the person has seen the preview and agreed. Each
        confirmation_id works once, within 15 minutes, for the person who asked for the preview. For a void or an
        edit, confirm_document must be the document number from the preview."""
        return run(ctx, "confirm_change", write.confirm, confirmation_id=confirmation_id,
                   confirm_document=confirm_document)

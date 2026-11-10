# MCP expansion: implementation plan

Spec: `docs/superpowers/specs/2026-11-10-mcp-expansion-design.md` (approved 2026-11-10). Extends the built MCP server in `mcpserver/`. Executed inline, task by task, test first. Superpowers skills are not used in this work.

## Global constraints

- Every write goes through the existing posting service for that document, and so through `ledger/posting.py`. No new ledger write path. No change to model guards or SQLite triggers.
- Every write tool is preview, then `confirm_change`, using `mcpserver/write.py` (`OPERATIONS`, dry run, roll back). A new tool adds a function `_name(user, payload, flags) -> (number, summary)` and an entry in `OPERATIONS`.
- Permissions match the screen. Voids and edits require Admin (`core.roles.is_admin`), checked before any lookup that could leak data.
- Records are found by visible numbers and names (`mcpserver/lookup.py`). Money is `Decimal` through `runtime.money`, shown with `runtime.fmt`.
- Reads return at most 50 rows (`read._limited`). Reports read from the same functions the screens use.
- Error text is plain language. Service errors pass through unchanged. New strings are listed in each task and tested by exact match.
- Tests go in `tests/test_mcp_reads.py`, `tests/test_mcp_writes.py`, `tests/test_mcp_voids.py`, reusing the fixtures in `tests/test_mcp.py` (move shared ones to `tests/conftest.py` only if needed, as a mechanical change).
- American English. No new dependencies. No secrets.

## Review focus

- A void or edit can never be confirmed by a non-admin, by another user's confirmation id, or without the exact `confirm_document`.
- A preview never leaves a row behind other than the `PendingChange`.
- Report numbers from the MCP equal the screen's on the same data.

## Tasks

### Task 1: Read tools

Files: `mcpserver/read.py`, `mcpserver/tools.py`, `tests/test_mcp_reads.py`.

Interfaces: `list_sales_orders(status)`, `get_sales_order(number)`, `list_customer_payments(customer, start, end)`, `get_customer_payment(number)`, `list_vendor_payments(vendor, start, end)`, `get_vendor_payment(number)`, `list_credits(kind, status)`, `list_bank_accounts()`, `get_bank_account(account)`, `list_stock_movements(sku, start, end)`, `get_account_ledger(account, start, end)`, `close_checklist(month)`.

- [x] Tests: each tool returns the same figures as the screen's query on shared data; unknown number gives "No sales order has the number SO-000999."; lists cap at 50 with the "more" note.
- [x] Implement, using the same balance helpers as the screens.
- [x] `close_checklist` calls `ledger.periods`, `items.reports.close_check`, `handling.checks.close_check` and returns their lines plus whether the month is closed. Read only.
- [x] Commit: `MCP: read tools for orders, payments, credits, bank accounts, ledger, close checklist`.

### Task 2: More reports in `run_report`

Files: `mcpserver/read.py`, `mcpserver/tools.py`.

Adds `general_ledger`, `bank_register`, `stock_movements`, `fifo_lots`, `negative_stock`, `customer_balances`, `vendor_balances`, `open_sales_orders`, `open_purchase_orders`, `undeposited_funds`, `drop_ship_awaiting_bill`, `unpaid_invoices`, `1099_summary`. New optional argument: `account` (for general ledger and bank register), `sku`.

- [x] Tests: for each, MCP totals equal the report function's totals on shared data; `general_ledger` without `account` says "Choose an account for the general ledger."
- [x] Implement by calling the function behind each screen in `ledger/reports.py`, `items/reports.py`, `sales/reports.py`, `purchasing/reports.py`, `banking/`.
- [x] Update `REPORTS` and the tool's `Literal`.
- [x] Commit: `MCP: thirteen more reports`.

### Task 3: New creates

Files: `mcpserver/write.py`, `mcpserver/tools.py`, `tests/test_mcp_writes.py`.

Tools and services: `create_sales_order` (`sales.posting.orders.create_order`), `create_credit_note` (`create_credit_note`, optional `apply_credit`), `create_vendor_credit` (`create_return_credit` or `create_allowance_credit`), `issue_refund` (`issue_refund`), `make_deposit` (`make_deposit`), `create_transfer` (`banking.transfers.create_transfer`), `adjust_stock` (`items.stock.record_movements`), `adjust_item_value` (`record_value_adjustment`).

- [x] Tests per tool: preview posts nothing; confirm posts exactly the preview; Standard user gets the app's permission result; closed month blocks; negative-stock warning flows through the existing flag for `adjust_stock`.
- [x] Implement each `_operation` and register it.
- [x] Commit per tool group: orders and credits, refund and deposit and transfer, stock and value adjustments.

### Task 4: Journal entry

Files: `mcpserver/write.py`, `mcpserver/tools.py`, `tests/test_mcp_writes.py`.

Tool `create_journal_entry(date, lines[{account, debit, credit, description}], memo, reference)`. Calls `ledger.posting.post_entry` (the same call as `journal_new`). Permission `ledger.add_journalentry` or Admin. Accounts by number or name.

- [x] Tests: unbalanced, inventory account ("Line 1: inventory accounts can only change through stock movements."), closed month, unknown account, and a good entry that previews the lines and posts on confirm. Standard user without the permission gets the role message.
- [x] Commit: `MCP: create journal entries`.

### Task 5: Voids and edits

Files: `mcpserver/write.py`, `mcpserver/tools.py`, `mcpserver/models.py` (migration only if a field is added to `PendingChange`), `tests/test_mcp_voids.py`.

Void tools: `void_invoice`, `void_customer_payment`, `void_bill`, `void_vendor_payment`, `void_credit_note`, `void_vendor_credit`, `void_refund`, `void_deposit`, `void_transfer`, `void_journal_entry`, `void_stock_movement`. Input: `number`, `reason`, optional `date`. Edit tools: `edit_invoice`, `edit_bill`, `edit_sales_order`, `edit_purchase_order`, taking the same line shapes as the create tools.

- [x] Admin check first. Standard user gets "Your role (Standard) can't do this. Ask an admin."
- [x] A void preview includes a plain-language `effect` line naming the document, its total, the entries it reverses, and applications that will be unapplied.
- [x] `confirm_change` takes optional `confirm_document`. For any void or edit it is required and must equal the previewed document number, else "Type the document number (INV-000123) to confirm this void."
- [x] Edit preview shows before and after.
- [x] Tests: non-admin refused at preview and at confirm; wrong or missing `confirm_document`; closed month; already void; applied payment shows in the effect; reversing entry posted through `ledger/posting.py`; audit log names the admin.
- [x] Commit per group: voids for sales documents, voids for purchasing documents, other voids, edits.

### Task 6: Settings filter, README, end to end

Files: `mcpserver/views.py`, the Claude connections template, `README.md`, `tests/test_mcp.py` (end to end).

- [x] The call log page gets a filter "Voids and edits".
- [x] README: update the MCP section's tool list and the "not available" line; keep the Status line current.
- [x] End-to-end test with the SDK client: list tools, run a new report, preview and confirm a credit note, preview and confirm a void with `confirm_document`.
- [x] Screenshots of the call log page with the filter, sent with `SendUserFile` (throwaway demo database).
- [x] Commit: `MCP: call log filter, README, end to end test`.

## Out of scope

Month close and reopen writes, bank inbox, reconciliation, check printing, imports, user and settings changes, hard deletes.

# Claude tools for the new features, and packing slips: design

Status: the owner chose this batch in chat on 2026-11-28 ("Claude tools + quick wins"); in review.

Follows `2026-11-28-books-gaps-design.md`. The Claude connector (MCP service, `mcpserver/`) did not cover the features merged in PR #60, and the warehouse has no packing slip.

Dropped from the batch: the "flaky" inbox test. Its one failure (`test_stale_candidate_is_refused`, an auditlog content-type error at teardown) came from model files being added while that test run was in progress. It passed in the clean full run and is not flaky.

## 1. Claude tools

Every tool follows the existing rules: reads are read-only; writes return a preview and a `confirmation_id`, and nothing is saved until `confirm_change`. The same permissions as the app apply.

### 1.1 Reports (`run_report`)
New report names, each calling the same function as the screen:

| Report | Inputs | Returns |
|---|---|---|
| `sales_by_customer` | start, end | rows (customer, invoices, sales, credits, net, written_off), totals, other_income, pnl_income, difference |
| `sales_by_item` | start, end, optional customer | rows (sku, name, quantity, amount, average), total, ledger_total, difference |
| `purchases_by_vendor` | start, end | rows (vendor, bills, purchases, credits, net), totals, ap_total, difference |
| `purchases_by_item` | start, end, optional vendor | rows (sku or account, name, quantity, amount, average), total, ledger_total, difference |
| `cash_flow` | start, end | net_income, noncash, operating, investing, financing (account, amount), section totals, net_change, cash_start, cash_end, bank_change, difference |

`run_report` gets two optional inputs, `customer` and `vendor` (exact name). An unknown name is refused with the existing lookup message.

### 1.2 Write-off (`write_off_invoice`)
Preview, then confirm. Inputs: invoice number, amount (default: the balance), reason, date (default: today). Admin only, as in the app; a standard user gets the existing role message. The preview shows the credit note number, the bad debt account, the posting lines, and the invoice balance after. It calls `sales.posting.writeoff.write_off`, so every app message applies unchanged.

### 1.3 Recurring schedules
- `list_recurring` (read): every recurring bill and recurring journal entry with kind, name, vendor (bills), frequency, start, end, amount, next date, status (Active, Paused, Ended), and last error.
- `create_recurring_bill` (preview, confirm): name, vendor, frequency, start date, optional end date, optional terms, notes, expense lines (account, amount, description). Same checks and messages as the screen (spec 3.6 of the books-gaps spec).
- `create_recurring_journal_entry` (preview, confirm): name, memo, frequency, start, optional end, lines (account, debit, credit, description). Debits must equal credits.
- `pause_recurring` and `resume_recurring` (preview, confirm): kind ("bill" or "journal_entry") and name. Resume skips dates passed while paused, as in the app.

A schedule's name is how Claude names it. Names are not unique in the app, so a name that matches more than one schedule of that kind is refused: "More than one recurring <kind> is named <name>. Rename one in the app first."

### 1.4 Out of scope
Editing or deleting schedules through Claude, attachments through Claude, voiding write-offs through Claude (the existing `void_credit_note` already does it once the application is removed in the app).

## 2. Packing slips

A packing slip for each invoice: what is in this shipment, without prices.

- **Where:** a Packing slip button on the invoice page, next to Download PDF. URL `/sales/invoices/<number>/packing-slip/`, a PDF, open to any signed-in user.
- **Layout:** the company block and logo as on the Classic invoice; "Packing slip" title; invoice number, date, sales order number, and expected delivery date when the order has one; Ship to; a table of SKU, Description, Quantity; total quantity; a "Received by" line with name, signature, and date. No prices, totals, balances, or payment terms anywhere.
- **Which lines:** stock lines only. Drop-ship lines are not in this box. When the invoice has drop-ship lines, a note below the table lists them: "Shipping separately from <vendor>: <SKU> × <qty>". An invoice with only drop-ship lines still prints, with the table empty and the note.
- Adjustment lines never appear. Void invoices print with a "VOID" mark, as invoice PDFs do.
- Not added: packing slips from sales orders, pick lists, shipment tracking numbers.

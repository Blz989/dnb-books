# Adjustments on bills and invoices

- **Date:** 2026-11-19
- **Status:** Decisions made by the owner (chat, 2026-11-19); in review.
- **Parent specs:** `2026-10-27-phase-3-sales-design.md`, `2026-10-27-phase-4-purchasing-design.md`, `2026-10-28-order-handling-design.md`, `2026-10-28-phase-5b-zoho-migration-design.md`.

## 1. Goal

A bill or invoice can carry one signed Adjustment amount, as in Zoho, so its total can be matched to the vendor's invoice (or rounded for a customer) without a separate vendor credit or credit note.

The case that prompted it: Nova's invoice 620562 came to 1,675.00 while BILL-02875's item lines came to 1,737.00. VC-00002 (62.00) was entered only to close that gap, and it later turned out to be 10.00 too large. With an adjustment field, BILL-02875 would carry -62.00 and the fix would be to change it to -52.00.

## 2. What exists today

- Bill and invoice posting already accept a negative non-stock line, but only when the Zoho import passes `allow_negative_adjustment=True`. Screens and Claude (MCP) tools reject negative amounts.
- The Zoho import stores Zoho's adjustment as an ordinary line: an invoice line on the non-stock item ADJ ("Adjustment", income account 4050 Sales, or a copy `ADJ-<account>` when Zoho named another account), and a bill expense line with the description "Adjustment".
- Totals and balances (`invoice_total`, `bill_total`, the SQL prefilters) sum the lines, so an adjustment stored as a line needs no change to balance math.
- Order profitability counts every invoice line as sales. Drop-ship cost comes only from bill item lines (`handling/profit.py`, `purchasing/dropship.py`), so vendor credits and bill expense lines do not change an order's cost.

## 3. Decisions from the owner

1. **Where.** Bills and invoices. Not purchase orders, sales orders, credit notes, or vendor credits.
2. **One adjustment per document**, signed (above or below zero), optional. Blank or 0.00 means none.
3. **Stored as a line, flagged.** A new `is_adjustment` flag on `BillExpenseLine` and `InvoiceLine`. At most one flagged line per document, always last. Balance math is unchanged.
4. **Bill adjustment account.** Purchasing settings get "Default adjustment account", set to 5010 Wholesale Cost of Goods Sold when that account exists. The bill form preselects it and the user can pick another account on any bill. Inventory and accounts payable accounts are refused, as for expense lines today.
5. **Invoice adjustment account.** Always the ADJ item's income account (4050 Sales today). No account choice per invoice. The owner changes it by editing the ADJ item. Editing an invoice keeps the adjustment's item, so an imported `ADJ-<account>` adjustment keeps posting to the account Zoho used.
6. **Totals.** Neither document's total may go below 0.00. A bill's total may not go below what is already applied (the existing rule).
7. **Stock.** An adjustment never moves stock and never changes an item's cost or FIFO lots.
8. **Invoice adjustments and order profit.** They count as sales on that invoice's order, as ADJ lines do now. They raise or lower profit and the handling fee.
9. **Bill adjustments and drop-ship order cost.** An adjustment entered in DnB Books on a bill that has drop-ship item lines is spread over the bill's item lines in proportion to their amounts, rounded to the cent, with any leftover cent going to the largest line. Each drop-ship line's share is added to the cost the order sees. The share on stock item lines is ignored for order cost. Changes reach handling through the existing owed vs. billed math, so the next handling run bills or credits the difference.
10. **Converted Zoho adjustments.** A data migration flags existing Zoho adjustments as adjustments, so old documents show them in the new totals row: an invoice's ADJ or `ADJ-*` line, and a bill's expense line described exactly "Adjustment" (only when the bill has one such line). Amounts and ledger entries do not change. These converted bill adjustments do **not** spread into order cost (a `spreads_to_orders` flag, false for converted lines, true for lines entered in DnB Books). Old orders' profits and handling stay as they are.

## 4. Screens

### 4.1 Bill form (new, edit, from purchase order)

Below the expense lines, above the total:

| Field | Notes |
|---|---|
| Adjustment | Signed amount, like -10.00 or 25.00. Blank for none. |
| Adjustment account | Dropdown, preselected from the default. |
| Adjustment note | Optional, defaults to "Adjustment". Shown on the bill page. |

The totals area reads: Subtotal (item and expense lines), Adjustment (only when there is one), Total.

### 4.2 Invoice form (new, edit, from sales order)

Below the lines: Adjustment (signed amount) and Adjustment note (optional, defaults to "Adjustment"). Totals: Subtotal, Adjustment (only when there is one), Total.

### 4.3 Document pages and PDFs

- Bill page and invoice page: the adjustment is not listed with the lines. It shows in the totals as "Adjustment" (or its note) between Subtotal and Total.
- Invoice PDFs, Classic and Modern: the same. Total quantity leaves the adjustment out.
- Revision history: the adjustment is part of each saved revision and shows in before and after.
- Customer statements, aging, and balance reports read totals and ledger lines, so they need no change.

### 4.4 Error messages

- "Adjustment: enter an amount like -10.00 or 25.00."
- "Adjustment: amounts can have at most 2 decimal places."
- "Adjustment: choose an account other than inventory or accounts payable."
- "The bill total can't be below zero."
- "The invoice total can't be below zero."
- Existing: "This bill has {applied} applied. The new total can't be less than that." (and the invoice equivalent).

## 5. Ledger

- Bill: the adjustment posts to its account with the rest of the bill. Below zero it credits that account; above zero it debits it. Accounts payable carries the adjusted total.
- Invoice: the adjustment posts to the ADJ item's income account. Accounts receivable carries the adjusted total.
- Edits re-post through the existing void-and-repost path in `ledger/posting.py`. No other code writes ledger lines.

## 6. Claude (MCP) tools

1. `create_bill` and `edit_bill` take `adjustment: {amount, account?, note?}`. `create_invoice` and `edit_invoice` take `adjustment: {amount, note?}`. On an edit, leaving `adjustment` out keeps the current one, and `{amount: "0.00"}` removes it.
2. `get_bill` and `get_invoice` return `subtotal`, `adjustment` (amount, account, note, or null), and `total`. Adjustment lines no longer appear in `expense_lines` or `lines`.
3. **`edit_bill` keeps omitted lines.** `item_lines` and `expense_lines` are optional in `edit_bill`, and today leaving one out removes every line of that kind. After this change, a list that is left out keeps the bill's current lines of that kind, and an empty list `[]` still removes them. (Found on 2026-11-19, when an `edit_bill` call that gave only expense lines would have dropped all 34 item lines on BILL-02875. The applied-amount check refused it.) The other edit tools require their `lines`, so they don't have this problem.

## 7. Out of scope

- Adjustments on purchase orders, sales orders, credit notes, vendor credits.
- Percentage discounts, per-line discounts, or more than one adjustment per document.
- A per-invoice adjustment account.
- Spreading adjustments into item cost, FIFO lots, or stock value.
- Spreading converted Zoho bill adjustments into order cost.
- Changing VC-00002 or BILL-02875. The owner fixes them in the app once the vendor's bill arrives.

## 8. Tests the plan must cover

- Bill and invoice create and edit with a positive, negative, and removed adjustment; ledger lines and balances match.
- The total-below-zero and below-applied refusals, with the exact messages above.
- At most one adjustment line per document; it stays last after edits.
- Drop-ship spread: proportional shares, cent rounding, a mixed stock and drop-ship bill, a converted Zoho adjustment that does not spread, and the handling owed difference after an edit.
- The data migration flags imported ADJ, `ADJ-*`, and single "Adjustment" expense lines and changes no amount or ledger line.
- Invoice PDFs (Classic and Modern) and bill and invoice pages show Subtotal, Adjustment, Total.
- MCP: adjustment in create, edit, and get; in `edit_bill`, omitted line lists kept and `[]` removes.
- Every ledger check stays 0.00 after the migration on the demo database.

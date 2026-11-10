# DNB Books MCP expansion: design

Status: draft for owner review. Decisions A and Admin-only voids settled 2026-11-10. Date: 2026-11-10. Extends `2026-11-04-mcp-server-design.md` (built, revision 4). That spec's section 5 listed voids, edits, refunds, credit notes, deposits, transfers, journal entries, and month close as "not available through the MCP". This spec changes that list. It does not change sign-in, the preview and confirm mechanism, or the security controls in sections 4 and 7 of the original, except where stated below.

## 1. Goal

Users can do from Claude what they can do in the app for everyday bookkeeping, not only look things up. Every write keeps the existing preview, then confirm, flow.

## 2. Owner decisions

| Topic | Decision |
|---|---|
| Read gaps | Add the missing read tools and reports (section 3). |
| New creates | Sales orders, customer credit notes, vendor credits, customer refunds, deposits, transfers, stock adjustments, value adjustments, and journal entries. |
| Voids and edits | Allowed through the MCP (this reverses the original "no voids" control). Guardrails in section 5. |
| Month close | Not requested. Stays in the app. A read-only close checklist is proposed (section 3). |

## 3. New read tools

| Tool | Returns |
|---|---|
| `list_sales_orders`, `get_sales_order` | Open or all; one order's lines with shipped and remaining quantities |
| `list_customer_payments`, `get_customer_payment` | Filter by customer, date; one payment with its applications |
| `list_vendor_payments`, `get_vendor_payment` | Same, for vendors |
| `list_credits` | Customer credit notes and vendor credits, open or all |
| `list_bank_accounts`, `get_bank_account` | Accounts with ledger balance, inbox count, unreconciled count |
| `list_stock_movements` | Filter by SKU and date |
| `get_account_ledger` | General ledger lines for one account and date range |
| `close_checklist` | For a month: the same list the Month close screen shows (`ledger/periods.py`, `items/reports.py:close_check`, `handling/checks.py:close_check`). Read only. |

`run_report` gains: `general_ledger`, `bank_register`, `stock_movements`, `fifo_lots`, `negative_stock`, `customer_balances`, `vendor_balances`, `open_sales_orders`, `open_purchase_orders`, `undeposited_funds`, `drop_ship_awaiting_bill`, `unpaid_invoices`, `1099_summary`. Each calls the report function behind the matching screen. Reports still read only posted ledger lines.

## 4. New write tools

Each is preview, then `confirm_change`, using the existing `mcpserver/write.py` dry-run and roll-back pattern. Each calls the posting service the screen calls.

| Tool | Posts through |
|---|---|
| `create_sales_order` | `sales.posting.orders.create_order` |
| `create_credit_note` | `sales.posting.receipts.create_credit_note`, optional `apply_credit` |
| `create_vendor_credit` | `purchasing.posting.credits.create_return_credit` or `create_allowance_credit` |
| `issue_refund` | `sales.posting.receipts.issue_refund` |
| `make_deposit` | `sales.posting.deposits.make_deposit` |
| `create_transfer` | `banking.transfers.create_transfer` |
| `adjust_stock` | `items.stock.record_movements` |
| `adjust_item_value` | `items.stock.record_value_adjustment` |
| `create_journal_entry` | See decision A below |

## 5. Voids and edits

The original spec removed voids from the MCP to guard against a wrong posting from chat. Allowing them needs stronger guardrails than a create has:

1. **Admin only.** Voids and edits require the Admin role in the MCP, even where the app lets a Standard user edit an unpaid invoice. (`void_entry` already requires an admin.)
2. **Void tools** take the document number and a required reason: `void_invoice`, `void_customer_payment`, `void_bill`, `void_vendor_payment`, `void_credit_note`, `void_vendor_credit`, `void_refund`, `void_deposit`, `void_transfer`, `void_journal_entry`, `void_stock_movement`. Each calls the existing void service, so voids stay reversing entries, and closed months still block them.
3. **Edit tools:** `edit_invoice`, `edit_bill`, `edit_sales_order`, `edit_purchase_order`. They call `edit_invoice`, `edit_bill`, `update_order`, `update_po`. The preview shows the change as a before and after, plus the voided and replacement entries the service will create.
4. **The preview names the effect** in plain language, for example "This will void INV-000123 ($1,250.00) and reverse its journal entry JE-000456. It has one payment applied, which will be unapplied."
5. **Higher bar for the confirm.** For voids, `confirm_change` also needs `confirm_document` set to the exact document number, so an agent cannot confirm a void from a stale or mixed-up preview.
6. **Audit:** the `McpCall` log marks these calls, and Settings → Claude connections gets a filter for "voids and edits".

Not added: deleting records, hard deletes of drafts, changes to settings, users, or chart of accounts, bank inbox actions, reconciliation, check printing, and the CSV imports.

## 6. Decision A: journal entries (owner chose A1)

You asked for draft journal entries. A finding changes the options. The app's Journal entries screen does not create drafts. `ledger/books_views.py:journal_new` calls `post_entry`, which posts at once. Drafts exist in the model and `post_draft`, but nothing creates one today.

| Option | What it takes | Trade-off |
|---|---|---|
| **A1. Post on confirm (recommended)** | `create_journal_entry` previews the lines, and confirm calls `post_entry`, the same call the screen makes. Needs the same permission (`ledger.add_journalentry` or Admin). No new ledger code. | The preview and confirm step is the review. The entry is live once confirmed. Fix a mistake by voiding. |
| **A2. Draft only** | Add `create_draft` to `ledger/posting.py` (a new ledger write path, so it needs a spec change and the same guards), a Draft badge on the Journal entries screen, and the app's Post action on it. | A second person can review before it posts. More code in the most protected module. |

Either way, the lines are validated by the existing `validate_lines`, `check_inventory_lines` (inventory accounts only change through stock movements), and `assert_open`. Accounts are named by number and name, never by id.

## 7. Month close

Not built as a write. The `close_checklist` read tool covers the useful part. Close and reopen stay in the app because they lock the books and change what every other write is allowed to do. If you want them in the MCP later, it needs its own decision: Admin only, the checklist shown in the preview, and "close anyway" never offered.

## 8. Errors and security

- Posting-service errors pass through unchanged, as before.
- Role messages follow the original table (section 6 there).
- The controls in section 7 of the original still apply. The row "A wrong posting from chat" now reads: preview and confirm on every write; voids and edits are Admin only and need the document number typed on confirm; closed months enforced; every write recorded in the audit log under the person's name.
- Because voids are now possible, the per-user "MCP access" switch stays off by default, and Settings → Claude connections shows the Admin-only note beside it.

## 9. Testing

- Each new write: works for Admin, role message for Standard where the app refuses, preview posts nothing, confirm posts exactly the preview, closed month blocks.
- Each void: Admin only, reason required, `confirm_document` must match, reverses through `ledger/posting.py`, closed month blocks, a document that already has applications shows the effect in the preview.
- Each new read tool and report: matches the screen's figures on the same data.
- Journal entry: unbalanced, inventory account, closed month, and unknown account refused with the app's messages.
- End to end: the SDK client against the ASGI app, including one void.
- Screenshots: Settings → Claude connections with the voids filter, and, if A2, the Draft badge and Post action.

## 10. Out of scope

Month close and reopen writes, bank inbox and reconciliation, check printing, imports, user and settings changes, hard deletes, emailing, anything from the original spec's out-of-scope list.

## 11. Build order

1. Read tools and reports (no write risk, ships first).
2. New creates (sales order, credits, refund, deposit, transfer, stock and value adjustments).
3. Journal entry (per decision A).
4. Voids and edits with their guardrails.
5. Settings filter, README, tests, screenshots.

## 12. Decisions and open questions

1. Decision A: **A1, post on confirm.** Settled.
2. Voids and edits: **Admin only.** Settled.
3. Open: should edits be limited to documents with no payments applied, or follow the app's rules exactly? Default if not answered: follow the app's rules exactly.

## 13. Addendum, 2026-11-10: drop-ship purchase orders

Asked after the first expansion merged (PR 46): the MCP could create drop-ship sales orders and bill a PO, but could not create the drop-ship PO. Owner approved adding it.

- New tool `create_drop_ship_pos(sales_orders, vendors, date)`. It calls `purchasing.posting.orders.create_drop_ship_pos`, the function behind Purchase orders, Drop-ship: one PO per vendor for the drop-ship lines not already on a PO, each PO line linked to its sales order lines. Same permission as the screen (any signed-in user).
- A line's vendor text that matches no vendor fails with the app's message ("No vendor named X. Choose a vendor for these lines."). `vendors` maps the text as typed to a real vendor name, which is what the screen's vendor picker does.
- Billing is unchanged: `create_bill` with `purchase_order` and item lines by SKU. The posting service treats a drop-ship PO line as drop-ship cost. A test now covers sales order, then PO, then bill through the MCP.

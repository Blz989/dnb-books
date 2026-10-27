# Phase 3: Sales

- **Date:** 2026-10-27
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (sections 4 and 5 apply). Where this document and the parent differ, this document wins for Phase 3.
- **Builds on:** Phase 1 (ledger), Phase 2 (items and stock engine, including the review fixes: close checks in `ledger.periods`), Docker deployment.
- **Visual references:** `docs/design/items-mockup.html` (screens), `docs/design/invoice-mockup.html` (invoice PDF).

## 1. Goal

Record everything DNB sells: customers, sales orders, invoices per shipment (including drop-shipped lines), customer payments, deposits, credit notes, and refunds, with every money movement posted to the ledger and every stock movement going through the Phase 2 engine.

### Success criteria (Phase 3 done when)

1. A scripted run through the app passes, with the AR and Undeposited Funds checks (section 6) at 0.00 after every step:
   1. Create a sales order with a stock line and a drop-ship line.
   2. Invoice it in two shipments, the second including the drop-ship line.
   3. Edit the first invoice's quantity.
   4. Receive a check payment and an ACH payment.
   5. Deposit the check.
   6. Issue a credit note and apply it.
   7. Refund an overpayment.
2. The invoice PDF matches `docs/design/invoice-mockup.html` (layout, type, colors), rendered by WeasyPrint.
3. Screenshots of every new screen and the PDF, light and dark where applicable, have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Sales flow | Sales order → one invoice per shipment. Partial shipments are common. No quotes. |
| Drop shipping | Per sales order line: ship from stock or drop ship (vendor named). Any item, stock or non-stock. Vendor bill usually arrives the same week. Cost is posted by the Phase 4 vendor bill. |
| Payments | Checks (deposited in batches), cash, ACH/wire. No cards. |
| Credits | Credit without a return (no stock comes back), applied to later invoices, or refunded |
| Customers | Payment terms. One default ship-to per customer, editable per order. No price lists, no credit limits. |
| Numbering | Continue Zoho's sequences (prefix and next number set at cutover) |
| Invoice PDF | Logo, company details, bill-to, ship-to, lines, totals, customizable notes. No bank details, no remittance stub, no customer PO field. No packing slip. |
| Editing saved invoices | Allowed: same number, reposted behind the scenes, history kept (section 5.3) |
| Month-close checks | Block, but an admin can close anyway with a reason, which is logged (section 6.3) |
| PDF engine | WeasyPrint (HTML/CSS to PDF; system libraries already in the Docker image) |

### Out of scope for Phase 3

Quotes, card payments, returns to stock, customer price lists or discounts, credit limits, multiple ship-to addresses per customer, customer statements, emailing documents, packing slips, sales tax, recurring invoices, the vendor and purchase order side of drop shipping (Phase 4), printed refund checks (Phase 5 check writer).

## 3. Modules

| App | Contents |
|---|---|
| `contacts` (new) | `Customer`. Vendors are added here in Phase 4. |
| `sales` (new) | Sales orders, invoices, payments, deposits, credit notes, refunds, numbering, settings, PDF, reports |

Rule carried from Phases 1 and 2:
- All sales postings go through `sales/posting.py`, which calls `ledger.posting` for money and `items.stock` for stock.
- Views never post directly.

## 4. Data model

| Model | Key fields |
|---|---|
| `contacts.Customer` | name, contact_name, email, phone, billing_address (text), ship_to_address (text, default for new orders), terms (DUE_ON_RECEIPT, NET_15, NET_30, NET_45, NET_60), resale_certificate (text), notes, is_active |
| `sales.SalesSettings` | single row: ar_account (subtype ACCOUNTS_RECEIVABLE), undeposited_account (subtype OTHER_CURRENT_ASSET), returns_account (INCOME subtype, "Sales returns and allowances"), company_name, company_address, company_phone, company_email, logo (image file stored under `/data/media`), default_invoice_notes |
| `sales.DocumentSequence` | doc_type (SO, INV, PMT, CN, DEP, REF), prefix (e.g. `INV-`), next_number, padding (digits) |
| `sales.SalesOrder` | number, customer, date, ship_to (text), notes, status (OPEN, CLOSED), closed_by, closed_at |
| `sales.SalesOrderLine` | order, item, description, quantity, price (4 dp), fulfillment (STOCK, DROP_SHIP), drop_ship_vendor (text), position |
| `sales.Invoice` | number, customer, date, terms, due_date, ship_to, notes, sales_order (nullable), revision (int, starts 1), status (OPEN, VOID), journal_entry (current revision's entry), voided_on, void_reason |
| `sales.InvoiceLine` | invoice, item, description, quantity, price (4 dp), amount (2 dp), so_line (nullable), fulfillment, drop_ship_vendor, stock_movement (nullable), drop_ship_billed (bool, set by Phase 4), position |
| `sales.InvoiceRevision` | invoice, revision, snapshot (JSON of header and lines before the change), edited_by, edited_at, reason |
| `sales.Payment` | number, customer, date, method (CHECK, CASH, ACH, WIRE), reference (check or transfer number), amount, deposited_to (account: Undeposited Funds for CHECK and CASH; a BANK account for ACH and WIRE), deposit (nullable), journal_entry, status (OPEN, VOID) |
| `sales.PaymentApplication` | payment, invoice, amount, applied_on (date), removed (bool, removed_on) |
| `sales.CreditNote` | number, customer, date, reason, journal_entry, status (OPEN, VOID) |
| `sales.CreditNoteLine` | credit_note, description, account (default returns_account), amount |
| `sales.CreditApplication` | credit_note, invoice, amount, applied_on, removed, removed_on |
| `sales.Deposit` | number, date, bank_account (subtype BANK), journal_entry, status (OPEN, VOID) |
| `sales.Refund` | number, customer, date, bank_account, method (CHECK, ACH), reference (check number typed), amount, source_payment or source_credit_note (one of the two), journal_entry, status |

Computed values (not stored):
- **Invoice total:** the sum of line amounts.
- **Invoice balance:** total, minus active payment applications, minus active credit applications.
- **Payment unapplied:** amount, minus active applications, minus refunds drawn from it.
- **Credit note unapplied:** total, minus active applications, minus refunds drawn from it.
- **Customer balance:** open invoice balances, minus unapplied payments and credits.

Applications are never deleted. Removing one sets `removed` and `removed_on`, so balances as of any past date can be rebuilt.

## 5. Posting rules

### 5.1 Documents

| Document | Journal entry (source_type) |
|---|---|
| Sales order | None |
| Invoice | Dr A/R (total); Cr each line item's income account (grouped by account) (`sales.invoice`). Stock lines also record a `SALE` movement per line through `items.stock.record_movements` with offset = item's COGS account and `source_type="sales.invoice"`. That posts Dr COGS, Cr Inventory at FIFO/average cost. Drop-ship lines record no movement. |
| Payment | Dr `deposited_to` (Undeposited Funds or bank), Cr A/R (`sales.payment`) |
| Payment application, credit application | None (both sides are A/R; subledger only) |
| Deposit | Dr bank, Cr Undeposited Funds for the sum of its payments (`sales.deposit`) |
| Credit note | Dr each line's account (default Sales returns and allowances), Cr A/R (`sales.credit_note`) |
| Refund | Dr A/R, Cr bank (`sales.refund`) |

A document whose total is 0.00 posts no entry.

**Verify before Phase 5:** Zoho's posting of COGS on drop-shipped lines, and whether its credit notes post to a contra-revenue account or directly to sales.

### 5.2 Sales orders and invoices

- **Invoicing from a sales order:** the form lists each line's remaining quantity (ordered minus invoiced on non-void invoices) and defaults to all of it. The user can lower it or set it to 0. Invoicing more than remains: "Line {n}: only {left} left to invoice on {so_number}."
- **Standalone invoices** (no sales order) are allowed.
- **Negative stock:** a stock line that would take an item below zero shows the Phase 2 warning and needs "Save anyway". Drop-ship lines never warn.
- **Due date:** invoice date plus the terms' days. "Due on receipt" means the invoice date. Terms default from the customer and are editable per invoice.
- **Closing a sales order:**
  - It closes automatically when every line is fully invoiced.
  - It can be closed by hand while lines remain. Remaining quantities then stop showing as open.
  - Reopening by hand is allowed.
- **Editing a sales order:** allowed while open. A line's quantity can't go below what's been invoiced: "Line {n}: {invoiced} already invoiced." Invoiced lines can't be deleted.

### 5.3 Editing a saved invoice

An edit keeps the same number and increments `revision`. In one transaction:

1. Save an `InvoiceRevision` snapshot of the current header and lines, with the user and an optional reason.
2. Void the current revenue entry (`ledger.posting.void_entry`, dated the invoice date).
3. Void each stock line's movement through `items.stock.void_movement`, called with a system flag so a Standard user's edit can do it. The flag is not exposed to views.
4. Post the new revenue entry and new stock movements from the edited lines, dated the (possibly new) invoice date.

Rules:
- The original and new invoice dates must both be in open months. Otherwise: "{Month YYYY} is closed. Choose a date in an open month or ask an admin to reopen it."
- The new total can't be below payments and credits already applied: "This invoice has {applied} applied. The new total can't be less than that."
- The customer can't change once anything is applied.
- Line edits respect the sales order's remaining quantities as in 5.2.
- The invoice page shows a "Revisions" list, and each revision opens its snapshot read-only.
- The PDF always shows the current revision.

### 5.4 Voiding

| Void | Allowed when | Message when blocked |
|---|---|---|
| Invoice | No active payment or credit applications | "{number} has payments or credits applied. Remove them first." |
| Payment | Not in a non-void deposit; no refund drawn from it | "{number} is in deposit {deposit}. Void the deposit first." / "{number} has a refund drawn from it. Void the refund first." |
| Deposit | Always (its payments return to undeposited) | |
| Credit note | No active applications or refunds | "{number} is applied to invoices. Remove the applications first." |
| Refund | Always | |

All voids are admin only, need a reason, and post reversing entries dated the void date (default the document date, must be an open month). Voided documents keep their numbers.

### 5.5 Payments, credits, and refunds

- **Receiving a payment:** the screen lists the customer's open invoices (oldest due first) with amount fields. "Apply oldest first" fills them in order. Applied total ≤ payment amount, and each application ≤ the invoice balance: "{invoice} has {balance} left to pay."
- **Unapplied amounts** stay on the customer as credit and can be applied later from the customer page. Removing an application is allowed when the invoice is not void.
- **Credit notes** are applied to invoices the same way.
- **Refunds** draw on one payment's or one credit note's unapplied amount: "Only {unapplied} is available to refund from {number}." Refunds are admin only. The method is check (with the check number typed) or ACH. Printing refund checks comes with the Phase 5 check writer.

### 5.6 Deposits

The deposit screen lists open, undeposited payments (method CHECK or CASH, not in a deposit). The user ticks the ones going to the bank, chooses the bank account and date, and sees the running total. The total is what Phase 5 will match to one bank statement line.

### 5.7 Numbering

- Each document type has its own sequence.
- A number is taken inside the saving transaction, so a failed save doesn't use up a number.
- Voided documents keep their numbers.
- Admins set the prefix and next number in Sales settings. The next number can't be set at or below a number already used with that prefix: "INV-004567 is already used. Choose a higher next number."

## 6. Checks and month close

### 6.1 Built-in checks

| Check | Rule |
|---|---|
| AR | Σ open invoice balances − Σ unapplied payment amounts − Σ unapplied credit note amounts = A/R account balance (ledger) |
| Undeposited | Σ amounts of non-void CHECK/CASH payments not in a non-void deposit = Undeposited Funds account balance |

Both are available as functions returning problem messages (like `items.stock.check_invariants`). They are run in tests and shown on the AR aging and Undeposited Funds reports as "Difference: 0.00".

### 6.2 Registered close checks

Sales registers two checks with `ledger.periods.register_close_check`: the AR and Undeposited checks above, evaluated at month end. Unpaid invoices and undeposited checks do **not** block a close. Only a mismatch between the documents and the ledger does.

### 6.3 Admin override (applies to every close check, including Phase 2's inventory check)

- `close_period(year, month, user, override_reason: str = "")`. When checks report problems and `override_reason` is non-empty and the user is an admin, the month closes anyway.
- The reason and the problems are saved on the period (`close_override_reason`, `close_override_problems`) and in the audit log.
- In the admin's close screen, a blocked close shows the problems and a "Close anyway" form that requires a reason.

## 7. Invoice PDF

- **Rendering:** a Django template (`sales/templates/sales/invoice_pdf.html`) rendered to PDF by WeasyPrint at `/sales/invoices/<number>/pdf/`. The filename is `<number>.pdf`.
- **Look:** must match `docs/design/invoice-mockup.html`. It uses the app's bundled fonts (no internet). The logo comes from Sales settings; with no logo, the company name is shown in Source Serif.
- **Notes:** the invoice's notes field is pre-filled from `default_invoice_notes` and editable per invoice.
- **Drop-ship lines** print the note "Shipped directly from the manufacturer".
- **Totals:** subtotal, then "Payments and credits" as of today, then balance due.
- **Void invoices** print with a large "VOID" watermark.
- **Dependencies:** WeasyPrint (from PyPI through uv, pinned in `uv.lock`). The Dockerfile already installs `libpango-1.0-0` and `libpangoft2-1.0-0`; Phase 3 adds `libharfbuzz-subset0`, which WeasyPrint uses for font subsetting. A test renders a PDF inside the test suite, so a missing library fails the build instead of the first real invoice.

## 8. Screens

All screens use the Ledger paper layout. The sidebar gains a **Sales** group: Customers, Sales orders, Invoices, Payments, Deposits, Credit notes. Reports go under Reports.

| Screen | URL | Notes |
|---|---|---|
| Customers | `/customers/`, `/customers/new/`, `/customers/<id>/`, `/customers/<id>/edit/` | List: name, balance, overdue badge. Detail: contact info, terms, open invoices, unapplied credits ("Apply" and "Refund" actions), history. Deactivating a customer with a balance: "{name} has a balance of {balance}. Settle it before deactivating." |
| Sales orders | `/sales/orders/`, `/sales/orders/new/`, `/sales/orders/<number>/`, `.../edit/`, `.../invoice/`, `.../close/` | Detail shows per line: ship from, ordered, invoiced, left. "Invoice a shipment" opens the invoice form pre-filled. |
| Invoices | `/sales/invoices/`, `/sales/invoices/new/`, `/sales/invoices/<number>/`, `.../edit/`, `.../pdf/`, `.../void/` | List filters: status, customer, overdue. Detail: lines, applications, revisions, journal entry links. |
| Payments | `/sales/payments/`, `/sales/payments/new/`, `/sales/payments/<number>/`, `.../void/` | |
| Deposits | `/sales/deposits/`, `/sales/deposits/new/`, `/sales/deposits/<number>/`, `.../void/` | |
| Credit notes | `/sales/credits/`, `/sales/credits/new/`, `/sales/credits/<number>/`, `.../apply/`, `.../void/` | |
| Refunds | `/sales/refunds/new/?payment=<number>` or `?credit=<number>`, `/sales/refunds/<number>/`, `.../void/` | Admin only |
| Sales settings | `/sales/settings/` | Admin only: accounts, company details, logo upload, default notes, numbering |
| Reports | `/reports/ar-aging/?as_of=`, `/reports/open-invoices/`, `/reports/customer-balances/`, `/reports/open-sales-orders/`, `/reports/undeposited-funds/`, `/reports/drop-ship-awaiting-bill/` | CSV export like other reports. AR aging buckets by days past due as of the date: Current, 1-30, 31-60, 61-90, Over 90. As-of balances use application and removal dates. |
| Home | | New cards: Open AR, Overdue, Undeposited, Drop shipments awaiting bill |

## 9. Permissions

| Action | Standard | Admin |
|---|---|---|
| View everything in sales | ✓ | ✓ |
| Customers; sales orders; create and edit invoices; receive and apply payments; deposits; credit notes and applications | ✓ | ✓ |
| Void any sales document; refunds; Sales settings and numbering; close a month anyway | | ✓ |

## 10. Testing

| Layer | Proves |
|---|---|
| Posting tests per document | Each produces the entry in 5.1; zero totals post nothing |
| Invoice from sales order | Remaining quantities, partial shipments, auto-close, manual close and reopen |
| Drop-ship lines | No stock movement or warning; listed on the awaiting-bill report |
| Invoice edit | Revision snapshot kept; old entries and movements voided; new ones posted; blocked below applied amount and in closed months; Standard user can edit |
| Applications | Overpayment becomes credit; credit applied later; removal keeps history; as-of AR aging before and after a removal |
| Voids | Every rule in 5.4, including deposit-then-payment ordering |
| Numbering | No gaps from failed saves; next number can't reuse a used number |
| Checks | AR and Undeposited checks hold after every step; a property test of random document sequences keeps both at 0.00 |
| Close | Blocked on mismatch; admin override with reason closes and records it; Standard user cannot override |
| PDF | Renders with WeasyPrint; text includes number, totals, notes; screenshot compared with the mockup |
| Views | Permissions per section 9; plain-language errors; no 500 on bad input |

The `finance:journal-entry` skill reviews 5.1 to 5.5, and `finance:reconciliation` reviews the AR and Undeposited checks.

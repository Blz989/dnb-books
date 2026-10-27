# Phase 4: Purchasing

- **Date:** 2026-10-27
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`. Where this document and the parent differ, this document wins for Phase 4.
- **Builds on:** Phase 1 (ledger), Phase 2 (stock engine), Phase 3 (sales: drop-ship lines, close checks with admin override, numbering, document patterns).
- **Visual reference:** `docs/design/items-mockup.html` (screens). Phase 4 has no printed documents.

## 1. Goal

Record everything DNB buys and owes:
- Vendors and purchase orders.
- Bills for stock, drop shipments, and expenses.
- Vendor payments by check, ACH, or company card.
- Vendor credits, including returns of stock.

It also completes drop shipping: the vendor bill posts the cost of a drop-shipped sale and clears it from the "awaiting vendor bill" report.

### Success criteria (Phase 4 done when)

1. A scripted run through the app passes, with the A/P, AR, inventory, and Undeposited Funds checks all at 0.00 after every step:
   1. Create a PO for stock.
   2. Receive and bill it in two parts.
   3. Create a sales order with a drop-ship line and invoice it, create its drop-ship PO, and bill it.
   4. Enter an expense bill.
   5. Pay three bills (one check, one ACH, one card), one of them partially.
   6. Return stock to a vendor for a credit and apply the credit to a bill.
2. The 1099 summary for the year shows the right totals: card payments excluded, that year's threshold applied.
3. Screenshots of every new screen, light and dark, have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Stock purchases | The bill arrives with the goods: receiving and billing are one step, and stock comes in at the bill's price |
| Purchase orders | Internal tracking only (ordered vs. received). No PO document is sent to vendors. |
| Paying vendors | Check (number recorded now; printed by the Phase 5 check writer), ACH, company credit card. Partial payments. |
| Other bills | Expense bills (rent, utilities, services). A bill may mix item lines and expense lines (freight goes to an expense account; no landed cost). |
| Vendor credits | Return stock to the vendor, credit without a return, apply to later bills. No vendor refunds. |
| Vendor details | Payment terms, default expense account, 1099 flag and tax ID |
| Return-to-vendor cost difference | Goes to the item's COGS account (no separate variance account) |
| Permissions | Standard users can enter bills and pay bills. Voids and settings are admin only. |

### Out of scope for Phase 4

- Sending PO documents.
- Landed cost.
- Recurring bills.
- Vendor refunds.
- Paying the credit card balance itself (a bank transfer, Phase 5).
- Printing checks (Phase 5).
- Bank reconciliation (Phase 5).
- 1099 e-filing or printing forms (Phase 4 produces a summary and CSV for the CPA).
- Multiple currencies.
- Approval workflows.

## 3. Modules

| App | Contents |
|---|---|
| `contacts` | `Vendor` added beside `Customer` |
| `purchasing` (new) | Purchase orders, bills, vendor payments, vendor credits, settings, numbering, reports, checks |

All purchasing postings go through `purchasing/posting/` (`orders.py`, `bills.py`, `payments.py`, `credits.py`). They call `ledger.posting` and `items.stock`, the same pattern as `sales/posting/`. Numbering reuses the Phase 3 `DocumentSequence` mechanism with new document types.

## 4. Data model

| Model | Key fields |
|---|---|
| `contacts.Vendor` | name (unique, case-insensitive), contact_name, email, phone, address, terms (Phase 3 `Terms`), default_expense_account (nullable, EXPENSE or COGS type), is_1099 (bool), tax_id (text; shown masked except the last 4 digits), notes, is_active |
| `purchasing.PurchasingSettings` | single row: ap_account (subtype ACCOUNTS_PAYABLE) |
| `purchasing.Threshold1099` | year (unique), amount. Seeded: 2025 = 600.00, 2026 = 2000.00. |
| `purchasing.PurchaseOrder` | number, vendor, date, expected_date (nullable), notes, status (OPEN, CLOSED), sales_order (nullable; set for drop-ship POs) |
| `purchasing.PurchaseOrderLine` | order, item, description, quantity, price (4 dp), so_line (nullable; drop-ship link to `sales.SalesOrderLine`), position |
| `purchasing.Bill` | number, vendor, vendor_invoice_number, date, terms, due_date, notes, purchase_order (nullable), revision, status (OPEN, VOID), journal_entry, voided_on, void_reason |
| `purchasing.BillItemLine` | bill, item, description, quantity, price, amount, po_line (nullable), kind (STOCK, DROP_SHIP, NON_STOCK), stock_movement (nullable), position |
| `purchasing.BillExpenseLine` | bill, account, description, amount, position |
| `purchasing.BillRevision` | bill, revision, snapshot (JSON), edited_by, edited_at, reason |
| `purchasing.DropShipAllocation` | bill_item_line, invoice_line (`sales.InvoiceLine`), quantity |
| `purchasing.VendorPayment` | number, vendor, date, method (CHECK, ACH, CARD), paid_from (account: BANK for check and ACH, CREDIT_CARD for card), check_number (CHECK only), reference, amount, journal_entry, status |
| `purchasing.BillPaymentApplication` | payment, bill, amount, applied_on, removed, removed_on |
| `purchasing.VendorCredit` | number, vendor, date, kind (RETURN, ALLOWANCE), reason, journal_entry, status |
| `purchasing.VendorCreditLine` | credit, item (RETURN) or account (ALLOWANCE), quantity (RETURN), amount (credit amount for the line), stock_movement (RETURN) |
| `purchasing.CreditApplication` | credit, bill, amount, applied_on, removed, removed_on |

New `DocType` values: `PO` (`PO-`, 5 digits), `BILL` (`BILL-`, 5), `VP` (`VP-`, 5), `VC` (`VC-`, 5). They are set at cutover to continue Zoho's sequences.

Computed values, with the same as-of rules as Phase 3 (documents count from their date until voided; applications count from `applied_on` until `removed_on`):
- **Bill balance:** total, minus active payment and credit applications.
- **Payment unapplied:** amount, minus active applications.
- **Credit unapplied:** total, minus active applications.
- **Vendor balance:** open bill balances, minus unapplied payments and credits.

## 5. Posting rules

### 5.1 Documents

| Document | Journal entry (`source_type`) |
|---|---|
| Purchase order | None |
| Bill | **Stock item lines:** each goes through `items.stock.record_movements(kind=RECEIPT, quantity=+q, unit_cost=price, account=<A/P>)`, which posts Dr Inventory, Cr A/P for that line. **All other lines:** one entry (`purchasing.bill`), Dr per line (grouped by account), Cr A/P for their sum. Drop-ship line: the item's COGS account (non-stock items: `InventorySettings.default_cogs_account`). Non-stock item line: the vendor's default expense account, else the COGS default. Expense line: its account. |
| Vendor payment | Dr A/P, Cr `paid_from` (bank, or credit card liability) (`purchasing.payment`) |
| Applications | None (subledger only) |
| Vendor credit, return | Per line: the item leaves through `record_movements(kind=RECEIPT, quantity=-q, account=<item COGS>)` at engine cost (Dr COGS, Cr Inventory). Then Dr A/P (credit amount), Cr COGS (credit amount) (`purchasing.vendor_credit`). Net effect: inventory down at engine cost, A/P down at the credit amount, and the difference in COGS. |
| Vendor credit, allowance | Dr A/P, Cr each line's account (`purchasing.vendor_credit`) |

Stock lines on a bill post through the stock engine's own entry, and the bill's entry covers the rest. The A/P total across both equals the bill total, and the A/P check (6.1) proves it.

A document with total 0.00 posts no entry.

### 5.2 Purchase orders and receiving

- **Lines and status:**
  - PO lines are stock or non-stock items with a quantity and an expected price.
  - Remaining = ordered − billed (non-void bills).
  - A PO closes automatically when everything is billed, or by hand (and can be reopened).
- **Billing a PO:** a bill created from a PO pre-fills each line's remaining quantity and the PO price. Both are editable. Billing more than remains: "Line {n}: only {left} left to receive on {po_number}."
- **Drop-ship POs:**
  - "Create drop-ship PO" on a sales order makes one PO per drop-ship vendor name for that order's drop-ship lines that are not already on a PO. The vendor is matched by name to a `Vendor` (case-insensitive). If there is no match, the user picks or creates the vendor first.
  - PO lines carry `so_line`. Bill lines from them are kind DROP_SHIP.
- **Phase 3 compatibility:** the sales order line's free-text `drop_ship_vendor` stays as is. The PO is the link to the real vendor.

### 5.3 Drop-ship allocation

When a bill with DROP_SHIP lines posts, each line's quantity is allocated to the customer invoice lines for the same sales order line, oldest first, up to each invoice line's not-yet-allocated quantity. An invoice line's `drop_ship_billed` becomes true once its whole quantity is allocated.

If the bill arrives before the customer is invoiced, the quantity stays unallocated. It is picked up by the next invoice for that sales order line: Phase 3's invoice posting calls `purchasing.dropship.allocate_pending(so_line)`.

Editing or voiding a bill removes its allocations and re-runs them.

### 5.4 Editing, voiding, duplicates

- **Edit in place, same as Phase 3 invoices:**
  - A snapshot is saved to `BillRevision`.
  - The old entry and stock movements are voided as system voids dated the bill date, then new ones are posted.
  - The new total can't be below what's applied: "This bill has {applied} applied. The new total can't be less than that."
  - The vendor can't change once anything is applied.
  - Both dates must be in open months.
- **Stock already sold:** editing or voiding a bill can remove stock that has already been sold. It then shows the Phase 2 negative-stock warning ("{sku} has {on_hand} on hand. Voiding this movement will take it to {new}. Continue?") and needs a confirm. Later sales are recosted by the engine.
- **Voids** are admin only, need a reason, and are blocked by active applications:
  - Bill: "{number} has payments or credits applied. Remove them first."
  - Credit: "{number} is applied to bills. Remove the applications first."
- **Voiding a payment** removes its applications on the void date, reopening the bills (same as Phase 3).
- **Duplicate vendor invoice number:** a non-void bill from the same vendor with the same `vendor_invoice_number` (case-insensitive, trimmed) gives the warning "{vendor} already has bill {number} with invoice # {vendor_invoice_number}. Save anyway?" and needs a confirm.

### 5.5 Paying bills

- **The Pay bills screen** lists open bills (filters: vendor, due by date) with a pay amount per bill. "Pay in full" pre-fills balances.
- **Recording:** one vendor payment per vendor selected, applied to that vendor's ticked bills.
- **Method:**
  - Check: needs a bank account and a check number per vendor. Check numbers must be unique per bank account among non-void payments: "Check {n} is already used on {account}."
  - ACH: needs a bank account.
  - Card: needs a CREDIT_CARD account.
- **Amounts:** partial amounts are allowed. An amount above the bill balance: "{bill} has {balance} left to pay."
- **Single payment screen:** one payment to one vendor, with any amount over the applied bills kept as vendor credit (unapplied payment). That covers deposits or prepayments to a vendor.

### 5.6 Vendor credits

- **Return:**
  - Lines pick items, quantities, and the credit amount the vendor gives.
  - The stock engine's negative-stock warning applies.
  - The default credit amount is quantity × the item's current unit cost.
- **Allowance:** lines pick an account (default: the vendor's default expense account) and an amount.
- **Applying:** credits apply to the vendor's bills the same way payments do.

### 5.7 1099 summary

- **For a calendar year,** per vendor with `is_1099`: the sum of non-void vendor payment amounts dated in that year with method CHECK or ACH. Card payments are excluded because they are reported by the card processor on Form 1099-K.
- **Threshold:** the threshold for the year comes from `Threshold1099`. Rows at or above it are marked "File". With no threshold for the year: "Set the 1099 threshold for {year} in Purchasing settings."
- **CSV** columns: vendor name, tax ID (full, admin only), address, total paid, file (yes/no).
- **Note shown on the report:** "Check amounts and thresholds with your accountant before filing."

## 6. Checks and month close

### 6.1 A/P check

Σ open bill balances − Σ unapplied vendor payments − Σ unapplied vendor credits = the A/P account balance (a credit-normal balance, shown positive). The message is "Vendor balances say {x}, the A/P account says {y}." Like Phase 3, it can be evaluated as of any date.

### 6.2 Registered close check

Purchasing registers the A/P check with `ledger.periods.register_close_check`. Open bills don't block a close; a mismatch between documents and the ledger does. The Phase 3 admin override applies.

## 7. Screens

All screens use the Ledger paper layout. The sidebar gains a **Purchasing** group: Vendors, Purchase orders, Bills, Pay bills, Vendor credits.

| Screen | URL | Notes |
|---|---|---|
| Vendors | `/vendors/`, `/vendors/new/`, `/vendors/<id>/`, `.../edit/` | List: balance, bills due. Detail: contact, terms, default account, 1099 info (tax ID masked), open bills, unapplied credits and payments with "Apply", history. Deactivating with a balance: "{name} has a balance of {balance}. Settle it before deactivating." |
| Purchase orders | `/purchasing/orders/`, `.../new/`, `.../<number>/`, `.../edit/`, `.../bill/`, `.../close/` | Detail: ordered, billed, left per line. Sales order detail gains a "Create drop-ship PO" button (sales app template change). |
| Bills | `/purchasing/bills/`, `.../new/`, `.../<number>/`, `.../edit/`, `.../void/` | Form: vendor, vendor invoice #, date, terms (default from vendor), from PO (optional), item lines, expense lines. Duplicate-number confirm. Detail: lines, applications, revisions, drop-ship allocations (invoice links). |
| Pay bills | `/purchasing/pay/` | As in 5.5. Shows the payments created, grouped by vendor. |
| Vendor payments | `/purchasing/payments/`, `.../new/`, `.../<number>/`, `.../void/` | Single payment and apply |
| Vendor credits | `/purchasing/credits/`, `.../new/?kind=return` or `allowance`, `.../<number>/`, `.../apply/`, `.../void/` | |
| Purchasing settings | `/purchasing/settings/` | Admin: A/P account, numbering, 1099 thresholds by year |
| Reports | `/reports/ap-aging/?as_of=`, `/reports/bills-due/?through=`, `/reports/vendor-balances/`, `/reports/open-purchase-orders/`, `/reports/1099-summary/?year=` | CSV like the other reports. A/P aging buckets and Difference card as in Phase 3's AR aging. |
| Home | | Cards: Bills due in the next 7 days (total), Overdue bills (total) |

## 8. Permissions

| Action | Standard | Admin |
|---|---|---|
| View everything in purchasing (tax IDs masked) | ✓ | ✓ |
| Vendors, purchase orders, enter and edit bills, pay bills, vendor payments, vendor credits and applications | ✓ | ✓ |
| Void any purchasing document; purchasing settings, numbering, 1099 thresholds; full tax IDs and 1099 CSV | | ✓ |

## 9. Testing

| Layer | Proves |
|---|---|
| Posting per document | The entries in 5.1; stock bills raise stock at bill price; return credits split engine cost vs. credit amount into COGS |
| Receiving | Remaining quantities, partial bills, auto-close, over-receiving refused |
| Drop ship | Drop-ship PO creation per vendor; bill posts COGS with no stock; allocation to invoice lines oldest first; bill before invoice is allocated by the later invoice; edit and void re-run allocations; awaiting-bill report updates |
| Bill edit and void | Revision snapshot; reposting; stock recosting of later sales; rules in 5.4 |
| Pay bills | One payment per vendor; partial; card from a credit card account; duplicate check numbers refused; overpayment kept as credit |
| 1099 | Card excluded; year boundaries; thresholds per year; missing threshold message; tax ID masking for Standard users |
| Checks | A/P check after every step; a property test of random purchasing actions keeps A/P, inventory, and AR checks at 0.00; close blocked on mismatch, override works |
| Views | Permissions per section 8; plain-language errors; no 500 on bad input; double submits post once |

The `finance:journal-entry` skill reviews 5.1, and `finance:reconciliation` reviews the A/P check.

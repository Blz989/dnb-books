# Phase 5b: Zoho migration and catch-up

- **Date:** 2026-10-28
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (Phase 5). Where they differ, this document wins.
- **Builds on:** Phases 1 to 5a and Order profitability and handling.
- **Source data:** the owner's Zoho export of 2026-10-28 (`D__B_Goods_Trading_LLC_2026-10-28.zip`), plus Zoho reports the owner exports for the tie-out (section 6). Neither is ever committed to the repository.

## 1. Goal

Move D & B Goods Trading off Zoho with its full history, and give the owner the tools to catch up the 2026 books inside DNB Books.

### What the export shows

| Area | State in Zoho |
|---|---|
| History | March 2020 to September 2026. 9,707 sales orders and 9,686 invoices (102,000 lines), 2,691 bills, 9,308 customer payments, 476 vendor payments, 2,338 items, about 3,968 contacts, 8 vendors, and 84 accounts with no account numbers. |
| Sales | Current through Aug 20, 2026 (invoices) and Sep 14, 2026 (customer payments). |
| Money out | Stops around Feb 20, 2026: vendor payments, expenses, transfers (card payoffs, line of credit), purchase orders, and stock receipts. |
| A/P | $135,031.58 open on 278 bills, mostly paid in reality. |
| Stock | 386 items below zero (−6,302 units in total), because stock purchases since February were not entered. |
| Bank | Zoho's bank feeds were reconciled through Dec 31, 2025. |
| Undeposited Funds | 2,081 customer payments ($134,398.46, 2020 to Feb 2025) went to Undeposited Funds. Only $5,899.00 left it, by two transfers. `Deposit.csv` holds other money in, not deposits of payments. |
| Handling | Mr. Joseph Cordon is the handling payee. Zoho has one bill per sales order ("Shipping for Order SO-…", account Shipping Expenses), 946 orders from 2024-01-31 to 2026-08-20, at 50% of the order's Zoho "Total Margin". Seven sales orders from June to August 2026 have no handling bill. |

### Success criteria (done when)

1. On the owner's real export, a dry run of `import_zoho` finishes, and its tie-out report shows 0.00 difference on:
   - every account in Zoho's Trial Balance
   - each year's profit and loss
   - A/R and A/P aging totals
   - per-item quantities on hand

   Inventory value differences are listed per item with the reason (section 6.3).
2. The automated tests pass on made-up sample files shaped like Zoho's. Real data is never committed.
3. The inbox's **Pay bills** and **Receive payment** actions work on bank, card, and loan rows, and every ledger check (inventory, A/R, Undeposited, A/P) stays at 0.00.
4. A Plaid link requests 365 days, and rows before an account's **Feed starts** date are set aside.
5. Screenshots of every new screen and the tie-out report have been sent to the owner.

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Backlog | Import Zoho as it is, then catch up in DNB Books from the bank feeds. |
| History | Everything, March 2020 onward, as full documents. |
| Approach | Replay every Zoho record through DNB Books' own posting functions, in date order. A dry run with a tie-out comes first. |
| Missing purchases | The owner enters them as bills after the import. A stock count settles what's left. |
| Tie-out | The owner exports Zoho's reports (section 6.1) before cancelling Zoho. |
| Bank history | Zoho is reconciled through 2025. DNB Books needs bank rows from Jan 1, 2026 only. |
| Catch-up tools | New inbox actions **Pay bills** and **Receive payment**. Splitting one bank row across several things is out of scope. |
| Handling rate | 50% historically (Zoho). 55% from DNB Books' go-live. |
| Handling start | The go-live date. Orders between Aug 20, 2026 and go-live, plus the 7 orders Zoho never billed, are billed by hand. |

### Out of scope

- Importing Zoho users, activity logs, attachments, email history, price lists, or empty modules (projects, estimates, recurring documents, composite items, timesheets).
- Sales tax features. The few tax amounts in Zoho come over as a plain line (section 4.2).
- Multiple warehouses. Zoho's two warehouse names become one location.
- Re-importing into a database that already has documents.
- Splitting a bank row.
- Re-billing historical handling at 55%.

## 3. Modules

New app `migration` (import only), plus small changes in `banking`.

| File | Responsibility |
|---|---|
| `migration/zoho/reader.py` | Opens the export zip (including the nested `Invoice.zip` and `Sales_Order.zip`), reads each CSV with its exact Zoho column names, and validates that required columns exist. |
| `migration/zoho/mapping.py` | Pure functions: Zoho row to DNB Books spec objects (account type and number, item, terms, payment method, line amounts). No database writes. |
| `migration/zoho/validate.py` | Checks references before anything posts: customers, vendors, items, and accounts named by documents; invoices named by payments; bills named by vendor payments. Duplicate document numbers. |
| `migration/zoho/replay.py` | Posts everything in date order through the existing functions (`create_order`, `create_invoice`, `create_bill`, `receive_payment`, `make_deposit`, `pay_vendor`, credits, `post_entry`, stock adjustments). |
| `migration/tieout.py` | Reads the Zoho reports and compares them with DNB Books' ledger and reports. Writes the tie-out report (HTML and PDF). |
| `migration/models.py` | `ZohoRecord(kind, zoho_id, content_type, object_id)` for traceability. `ImportRun(started_at, finished_at, dry_run, export_name, summary JSON, report_pdf)`. |
| `migration/management/commands/import_zoho.py` | The command. |
| `banking/inbox.py`, `banking/views/inbox.py` | **Pay bills** and **Receive payment** actions. |
| `banking/plaid_client.py`, `banking/models.py`, `banking/sync.py`, `banking/importers.py` | `days_requested=365`; `BankAccount.feed_starts`; earlier rows set aside. |

## 4. Mapping rules

### 4.1 Master data

- **Accounts.** Zoho has no account numbers. Numbers are assigned by Zoho account type, in steps of 10, ordered by name within each range. The importer prints the mapping, and the owner can rename accounts afterwards.

  | Zoho type | DNB Books type and subtype | Numbers |
  |---|---|---|
  | Bank, Cash | Asset, Bank | 1000–1099 |
  | Accounts Receivable | Asset, A/R | 1100 |
  | Other Current Asset, Payment Clearing | Asset, Other current asset | 1200–1299 |
  | Stock | Asset, Inventory | 1300–1399 |
  | Fixed Asset | Asset, Fixed asset | 1500–1599 |
  | Accounts Payable | Liability, A/P | 2000 |
  | Credit Card | Liability, Credit card | 2100–2199 |
  | Other Current Liability | Liability, Other current liability | 2200–2399 |
  | Other Liability | Liability, Long-term liability | 2500–2599 |
  | Equity | Equity | 3000–3099 |
  | Income | Income | 4000–4099 |
  | Cost Of Goods Sold | Cost of goods sold | 5000–5099 |
  | Expense | Expense | 6000–7999 |
  | Other Expense | Other expense | 8000–8099 |

  Inactive Zoho accounts come over inactive. "Undeposited Funds" becomes the Sales settings Undeposited account. "Accounts Receivable" and "Accounts Payable" become the A/R and A/P settings accounts.
- **Bank accounts.** A `BankAccount` is created for each active Zoho Bank and Credit Card account, plus "Line Of Credit - Headway" as a LOAN account. Zoho's "Credit Card" accounts named "Line Of Credit…" are created as LOAN.
- **Items.** These Zoho fields come over:
  - SKU, name, description, and status (Active or Inactive)
  - selling price (`Rate`) and purchase cost (`Purchase Rate`)
  - category (`Category Name`), vendor (`Vendor`), and vendor part number (`CF.Vendor SKU`)
  - income, COGS, and inventory accounts, by name

  Zoho "Inventory" items become stock items with FIFO costing. "Sales and Purchases" items become non-stock items.
- **Opening stock.** `Opening Stock` and `Opening Stock Value` come in as an opening movement dated the day before the earliest Zoho document (2020-02-01).
- **Customers and vendors.** From `Contacts.csv` and `Vendors.csv`: name, terms, addresses, phone, email, and 1099 flag. "Mr. Joseph Cordon" becomes the handling vendor.

### 4.2 Documents

- **Numbers.** Zoho numbers are kept. Each sequence continues after the highest imported number.
- **Sales orders.** Status `void` comes over void. Lines use `QuantityOrdered` and `Item Price`, and the line amount is `Item Total`.
- **Invoices.**
  - Linked to their sales order by `Sales Order Number`, and posted with `create_invoice`.
  - Line amount = `Item Total` (after discount). Stock lines reduce stock at FIFO cost.
  - The invoice-level fields become extra lines:
    - `Shipping Charge` on a non-stock item **Shipping charge**, posting to Zoho's Shipping Charge income account
    - `Adjustment` on a non-stock item **Adjustment**, posting to the account named in `Adjustment Account` or else Sales
    - `Item Tax Amount` on a non-stock item **Sales tax (Zoho)**, posting to Tax Payable

    So every invoice total matches Zoho to the cent.
- **Bills.** Item lines add stock at the bill's `Rate`, with the line amount `Item Total`. `CF.Sales Order` is kept in the bill's notes. Cordon bills stay plain bills on Shipping Expenses.
- **Purchase orders** come over closed, for reference. Stock enters through bills only, as in Phase 4.
- **Customer payments.**
  - A payment deposited to Undeposited Funds is CASH or CHECK (from Zoho's `Mode`), so it lands in Undeposited.
  - A payment deposited to a bank or Petty Cash is ACH (Zoho's Mode is kept in the reference), so it posts straight to that account.
  - Applications come from `Amount Applied to Invoice`. `Unused Amount` stays unapplied on the payment.
  - `Bank Charges` posts to Bank Fees and Charges.
- **Other deposits** (`Deposit.csv`) are not deposits of customer payments. They are other money in: line of credit draws, owner capital, refunds of bounced payments, and late fees. Each becomes a journal entry: debit `To Account`, credit `From Account`. Source type `migration.deposit`.
- **Undeposited Funds.** Zoho moved only two amounts out of Undeposited Funds, both by transfer ($5,899.00). Each transfer is paired with undeposited payments dated on or before it, oldest first, whose amounts add up exactly to the transfer. The pair becomes a real deposit (`make_deposit`), so the Undeposited check holds.
  - A transfer with no exact set is listed, and during the dry runs it is resolved with `deposit_pairs.csv` (transfer date and amount, then payment numbers).
  - The other customer payments Zoho left in Undeposited Funds stay there, about $128,000 from 2020 to February 2025, as in Zoho. The report lists them as a catch-up item for the owner and the accountant (section 4.4).
- **Vendor payments** are paid from Zoho's `Paid Through`, with method from `Mode` and check number from `Reference Number`. Bill applications come from `Bill Amount` per bill. Check numbers are recorded in the check-number registry as handwritten.
- **Credit notes and vendor credits** come over with their applications (`Creditnotes_Invoice.csv`, and the application rows in `Vendor_Credits.csv`).
- **Expenses** become journal entries: debit `Expense Account`, credit `Paid Through`. Source type `migration.expense`.
- **Journals** become journal entries line by line. Source type `migration.journal`.
- **Transfers** (`Transfer_Fund.csv`) become journal entries between the two accounts. Source type `migration.transfer`.
- **Inventory adjustments** become quantity adjustments, or value adjustments where Zoho's type is value, with Zoho's reason and account.
- **Warehouses.** Ignored, one location.
- **Opening balances.** Zoho's account opening balances are not in the export. If the tie-out shows a constant difference on balance-sheet accounts from the first date, the owner copies Zoho's Opening Balances screen into `opening_balances.csv` (account, debit, credit) and the importer posts it as one entry dated 2020-02-01.

### 4.3 Order of posting

Day by day, oldest first. Within a day:
1. sales orders and purchase orders
2. bills (stock in) and inventory adjustments that add stock
3. invoices
4. customer and vendor payments, credits, and applications
5. deposits
6. expenses, journals, and transfers
7. inventory adjustments that remove stock

Ties go by Zoho number. Negative stock is allowed during the import, as Phase 2 allows with a warning. Every warning is counted in the report.

### 4.4 After the replay

- **Reconciliation at Dec 31, 2025.** For each bank, card, and loan account with lines by then, the importer creates a reconciliation dated 2025-12-31, IN_PROGRESS, with every line through that date ticked and the statement balance left blank.
  - The owner types the statement balance and unticks any check still outstanding at year end.
  - Then the owner finishes it.
- **Undeposited Funds.** The report lists every payment still in Undeposited Funds, by year. The owner decides with the accountant how to clear them; a common cause is cash that was banked but recorded another way. The importer does not guess.
- **Handling.** Handling settings are left for the owner. The tie-out lists the 7 sales orders with no Cordon bill, with each one's profit at 50% and 55%.
- **Months.** All months are left open. The owner can close them after the catch-up.

## 5. The command

```
import_zoho <export.zip> --reports <folder> [--dry-run] [--opening-balances <csv>] [--deposit-pairs <csv>]
```

- **Refuses a database that has documents:** "The database already has documents. Import only into a new, empty database."
- **Validates first.** All validation errors are listed at once, before anything posts.
- **It always imports into a copy.** The database file is copied, the import runs into the copy one transaction per day, and the report is written. On success, a real run replaces the database file with the copy, after a backup through the existing backup service. A dry run deletes the copy. Any error stops the run, names the Zoho file and row, and deletes the copy, so the real database is never half imported.
- **`--dry-run`** writes the report and keeps nothing else.
- **Progress** is printed per month. The run summary (counts per document type, warnings, and differences) is saved on `ImportRun`.
- **Time.** It is expected to take tens of minutes. It runs in the `app` container: `docker compose exec -u dnb app python manage.py import_zoho ...`.

## 6. Tie-out

### 6.1 Reports the owner exports from Zoho

Export as CSV where Zoho offers it, otherwise XLSX or PDF. During the dry run, Claude converts any non-CSV file once, in the session, and the converted CSV is what the importer reads.

1. Trial Balance as of the export date.
2. Profit and Loss for each calendar year, 2020 through 2026 to date.
3. A/R Aging Summary and A/P Aging Summary as of the export date.
4. Inventory Summary (quantity on hand per item) and Inventory Valuation Summary as of the export date.

### 6.2 What is compared

| Check | Must be |
|---|---|
| Each Trial Balance account, DNB Books vs Zoho | 0.00 difference |
| Profit and loss, each year, income and expense totals and net | 0.00 |
| A/R and A/P aging totals | 0.00 |
| Quantity on hand per item | 0 difference |
| Document counts and totals per type | Equal |

### 6.3 Inventory value

DNB Books recomputes FIFO from every receipt and sale. Zoho's values differ where stock went negative. The report lists each item's value in both systems, the difference, and whether the item was ever negative. The differences post nowhere; the stock count and missing bills after go-live correct them. This is the only difference allowed.

### 6.4 The report

HTML and PDF, saved on `ImportRun`. It contains:
- a summary
- each check from 6.2 with pass or fail
- the differences, account by account and item by item
- negative-stock warnings by item
- unmatched deposits
- the 7 orders with no handling bill
- the account number mapping

## 7. Catch-up tools

### 7.1 Plaid history and Feed starts

- `create_link_token` requests `transactions.days_requested = 365`. Plaid's default is 90 and its maximum is 730. 365 covers Jan 1, 2026 onward.
- `BankAccount.feed_starts` is a date, default 2026-01-01, editable on the bank account form.
  - Rows dated before it, from Plaid or a file, are saved EXCLUDED with reason "Before the feed start date." They can be restored like any excluded row.

### 7.2 Pay bills (inbox, money out)

- Available on a NEW row with amount < 0 on a bank, card, or loan account.
- **The screen.**
  1. Choose the vendor.
  2. The vendor's open bills are listed oldest first, with their balances.
  3. Enter an amount on each bill to pay. "Fill oldest first" fills the amounts up to the row's amount.
- **Rules.**
  - The amounts must add up to the row's amount: "The bills you chose add up to {x}. The bank shows {y}."
  - The method is CHECK when the row has a check number (recorded in the check-number registry as handwritten, so the usual "already used" rule applies), CARD on a card account, otherwise ACH.
  - The date is the row's date.
- **Result.** One vendor payment through the existing pay-bills function, a `Match` to the row, and the row becomes MATCHED. Voiding the payment returns the row to the inbox, by the Phase 5a void hook.

### 7.3 Receive payment (inbox, money in)

- Available on a NEW row with amount > 0 on a bank account.
- **The screen.** Choose the customer, then enter amounts on open invoices, oldest first. "Fill oldest first" works the same way.
- **The method.**
  - ACH (the default) or WIRE: one payment straight to the bank, matched to the row.
  - CHECK: the payment goes to Undeposited Funds. Then a one-payment deposit on the row's date moves it to the bank, and the deposit is matched to the row.
- **Rule.** The amounts must equal the row: "The invoices you chose add up to {x}. The bank shows {y}."

## 8. Screens

All match "Ledger paper", with the Auto/Light/Dark toggle.

| Screen | Change |
|---|---|
| Inbox row | Two new choices next to Match, New entry, and Transfer: **Pay bills** (money out) and **Receive payment** (money in). |
| Pay bills from a row | Row summary, vendor select, open bills table with amount inputs, "Fill oldest first", total vs. bank amount, Save. |
| Receive payment from a row | Row summary, customer select, method, open invoices table, "Fill oldest first", total vs. bank amount, Save. |
| Bank account form | New field **Feed starts**. |
| Import runs (Admin) | A list of `ImportRun`s with the summary and a report link. Read only. |
| Tie-out report | HTML page and PDF. |

## 9. Testing

| Area | Tests |
|---|---|
| Reader | Nested zips; required columns missing gives a clear error; BOM and quoted commas. |
| Mapping | Account numbering by type; item types; terms; payment method by deposit account; shipping, adjustment, and tax lines; discount via `Item Total`. |
| Validation | Unknown customer, item, account, invoice, or bill reported with file and row; all errors listed at once. |
| Replay | A small made-up export (two years, 30 orders, drop-ship and stock, bills, payments to Undeposited and to the bank, a deposit, credits, an expense, a journal, a transfer, an adjustment, negative stock) imports, and every ledger check stays 0.00. Invoice totals equal the sample's. Numbering continues. `ZohoRecord` exists for every document. |
| Tie-out | Matching reports pass. A deliberately wrong Trial Balance figure fails with the account named. The inventory value difference is listed, not failed. |
| Command | Refuses a non-empty database; the dry run leaves the database untouched; a failing row leaves the database untouched; the Dec 31 reconciliations are created in progress. |
| Undeposited | A Zoho transfer out of Undeposited Funds becomes a deposit of the exact payments; no exact set is listed; `deposit_pairs.csv` resolves it. |
| Pay bills | Exact total required; method CHECK, CARD, or ACH by row; check number registry; matched; void returns the row. |
| Receive payment | ACH and CHECK paths; the check path creates a one-payment deposit matched to the row; exact total required. |
| Feed starts | Plaid and file rows before the date are excluded with the reason and can be restored. The link token includes 365 days. |

## 10. Catch-up guide (goes in the README)

1. Back up. Run `import_zoho --dry-run` and read the tie-out. Run it for real when it is clean.
2. Enter the missing purchase bills from February 2026 onward.
3. Enter sales after Aug 20, 2026.
4. Finish the Dec 31, 2025 reconciliation for each bank and card account.
5. Link the banks with Plaid (or import statement files). Set each account's Feed starts date if it isn't Jan 1, 2026.
6. Work the inbox oldest first: Pay bills, Receive payment, transfers and card payoffs, and expenses by rule.
7. Reconcile each month of 2026.
8. Set up handling (Cordon, a new Handling expense account, start date = go-live, 55%). Bill the listed unbilled orders by hand.
9. Count stock and enter the count.
10. Cancel Zoho once the owner is satisfied.

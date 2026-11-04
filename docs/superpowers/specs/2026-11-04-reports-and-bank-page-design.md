# Sidebar width, FIFO lot tracking, unpaid invoices, and the bank account page

Status: approved by the owner in conversation, 2026-11-04 (three decisions confirmed: store the bank-reported balance from feeds; show unapplied credits per customer; FIFO lot tracking for FIFO items only).

Builds on the system spec (`2026-10-27-zoho-replacement-design.md`), Phase 2 (items), Phase 3 (sales), Phase 5a (banking), and the perf PR (open balances in SQL). Nothing here posts to the ledger. All money is `Decimal`. Reports that show ledger money read `JournalLine.objects.in_ledger()` only.

## 1. Sidebar width

The company name in the sidebar truncates ("D&B Goods Tradin…").

- Widen the sidebar column from 236px to 264px.
- The brand name may wrap to two lines instead of truncating with an ellipsis (`white-space: normal`, clamp at 2 lines). The logo tile is unchanged.
- Under 900px the layout already stacks to one column; it must still do so with no horizontal scroll at 390px width.

## 2. FIFO Cost Lot Tracking report

A tracing report for one FIFO stock item over a date range, modeled on Zoho's report of the same name. URL `/reports/fifo-lots/`, slug `fifo-lots`, Inventory group, title "FIFO cost lot tracking", description "Each receipt of one item and the sales that used it."

### Inputs
- Item (required): a select of active and inactive FIFO stock items, labeled "SKU · name", ordered by SKU. No "all items" option.
- From, To (required, defaults: January 1 of this year to today). From must be on or before To.
- With no item chosen the page shows the form and the empty state "Pick an item to trace its cost lots."
- A non-FIFO item (reachable only by URL) shows the error "Lot tracking needs an item that uses FIFO costing."

### How lots are traced
The FIFO engine (`items/costing.replay`) gains an optional `trace` list argument. When given, replay appends events as it costs each move, without changing any result:
- `Consume(layer_id, outflow_id, quantity, cost)` when an outflow takes units from a layer (layer_id is the inflow movement id that created the layer).
- `Short(outflow_id, quantity, estimate)` when an outflow takes more than is on hand.
- `Fill(inflow_id, outflow_id, quantity, cost)` when a later inflow fills an earlier shortfall.

The report replays the item's movements that count on the To date (dated on or before To and not voided on or before To), in (date, id) order, with a trace. Because it is the same engine the books use, the pairing is exactly how cost was assigned.

### Layout
Header: "FIFO cost lot tracking", item name, SKU, the date range. Then KPI tiles: Received (units in range), Sold (units out in range), On hand at To, Value at To.

Main table, one row per pairing, two column groups:
- PRODUCT IN: Date, Transaction (e.g. "Bill BILL-00045" linking to the bill; "Opening stock", "Quantity adjustment", "Physical count", "Zoho import" without links), Received from (vendor name, or the movement reason), Quantity with unit, Age (days from the receipt date to To, or to the date the lot ran out if it ran out by To), Cost per unit (4 dp), Total (quantity × unit cost, 2 dp).
- PRODUCT OUT: Date, Transaction ("Invoice INV-000123" linking to the invoice; "Vendor credit VC-…" linking to it; adjustments and counts by name), Dispersed to (customer name, vendor name for returns, or the reason), Quantity dispersed.

Lots shown: every lot received in the range, plus every lot received before From that still had units at From (marked "Opening" with "N on hand at From"). For a lot shown, consumptions dated in the range are listed; consumptions before From are summarized in the first row as "N used before From". A lot taken by several outflows is several rows (the PRODUCT IN cells only on its first row); an outflow drawing on several lots appears under each lot.

### Gaps made visible
- A lot with no consumption in the range and units left at To: the PRODUCT OUT cells show the tag "No invoice yet".
- An outflow that sold stock not yet received (a Short event in range): listed in a second table, "Sold with no matching receipt", with date, transaction link, dispersed to, quantity short, estimated cost, and how it was resolved: "Filled by Bill BILL-… on date" (from a Fill event dated by To) or the tag "Still estimated".
- Lots filled late show the outflow in their PRODUCT OUT rows with the tag "Sold before received".
- Closing line: "On hand at {To}: {qty} {unit}, value {value}" taken from the inventory valuation calculation for that item on To, and "Lots remaining: {qty}, {value}" from the trace. If they differ, an error banner: "The lots add up to {lots value} but Inventory valuation shows {valuation value} for this item on {To}. Difference: {difference}."

### Exports
CSV (`?format=csv`, file `fifo-lots-{SKU}-{from}_{to}.csv`) with one row per pairing, columns: In date, In transaction, Received from, In quantity, Unit, Age, Cost per unit, In total, Out date, Out transaction, Dispersed to, Out quantity, Note. Shortfall rows follow, then a closing row. PDF through `core.report_pdf.pdf_export`, like every report.

## 3. Unpaid invoices

### 3a. Invoices list "Unpaid" filter
Add a checkbox "Unpaid only" (`?unpaid=1`) beside "Overdue only". It keeps every open, non-void invoice whose balance due is above zero today, computed in SQL with the perf PR's `invoices_with_open_balance` gap (balance > 0.005). It combines with the other filters and keeps pagination. The existing overdue filter is unchanged.

### 3b. Report "Unpaid invoices by customer"
URL `/reports/unpaid-invoices/`, slug `unpaid-invoices`, Receivables group, title "Unpaid invoices by customer", description "Each customer's unpaid invoices, aged, for collections."

- Input: As of (default today). All customers, always.
- For each customer with a nonzero A/R aging balance, sorted by name: a customer heading row, then one row per unpaid invoice (Invoice # linking to it, Date, Due date, Days past due (0 if not due), and the balance in exactly one bucket column: Current, 1-30, 31-60, 61-90, Over 90), then, if the customer has unapplied payments or credit notes, a row "Unapplied payments and credits" in a Credits column (negative), then a subtotal row "Total {customer}" per bucket plus Credits and Balance.
- Grand total row per bucket, Credits, and Balance. KPI tiles: Total unpaid (grand Balance), A/R account (ledger), Difference. The error banner from A/R aging when Difference is not zero.
- Numbers come from the same code A/R aging uses: `sales/reports.py` is refactored so A/R aging and this report share one function that yields each open invoice's balance and bucket, and each customer's unapplied credits, as of a date. Grand Balance equals A/R aging's total and the A/R account balance on the same date.
- CSV `unpaid-invoices-{as_of}.csv`: Customer, Invoice, Date, Due, Days past due, Current, 1-30, 31-60, 61-90, Over 90, Credits, Balance; subtotal and total rows. PDF via `pdf_export`. The PDF prints the customer heading and subtotal on each group so the salesman can read it on paper.

## 4. Bank account page

URL `/banking/<pk>/` (name `banking:account-detail`), one page per bank account, modeled on Zoho Banking.

### 4.1 Bank-reported balance (new, small model change)
`BankAccount` gains `bank_balance` (Decimal 14,2, null) and `bank_balance_on` (date, null), stored with the same sign as the Bank accounts list balance (assets: money in the account; cards and loans: amount owed, positive).
- Plaid sync: when a `/transactions/sync` page carries `accounts`, the account's `balances.current` and today's date are stored for each mapped account.
- OFX/QFX import: the statement's LEDGERBAL amount and date are stored (negated for card and loan accounts, since OFX shows what is owed as negative).
- CSV import does not set it.

### 4.2 Header
Account name, masked number ("···· 1234"), kind. Tiles:
- "Amount in books": the ledger balance (same sign rules as the Bank accounts list).
- "Amount in bank": `bank_balance` with "as of {date}". When it is not set, the last finished reconciliation's statement balance with "statement, {date}". When neither, "Not reported".
- "Last feed date": the newest bank transaction date for the account, or "None yet".
- "Bank feeds last fetched on {datetime}" for Plaid-linked accounts, with a "Sync now" button (admin, existing `banking:sync-now`, returning to this page). File-only accounts show "File import" and an "Import statement" link.

Buttons: "Add transaction" (existing new journal entry form), "Record deposit" (existing new deposit form), "Quick categorize" (the inbox filtered to this account). No new posting paths.

### 4.3 Tabs (`?tab=dashboard|uncategorized|all`, default dashboard)
**Dashboard**
- Activity summary cards: Last import (newest file-import row's created time, "Import statement" link); Last reconciliation (statement date of the last finished one, "Reconcile" link to start one); Transaction rules (count of active rules for this account or all accounts, link to Rules).
- 30-day closing balance chart: the ledger balance at the end of each of the last 30 days, drawn with `core.charts.line` and `core/_chart.html`, computed from one opening sum plus one grouped-by-date sum.
- "Total uncategorized" (NEW rows) and "Total recognized" (MATCHED plus BOOKED rows) counts.
- Recent transactions: the newest 10 register rows, with "View more" to the All transactions tab.

**Uncategorized** — label "Uncategorized ({count})", count of NEW rows. The existing inbox, filtered to this account, rendered by the same view code and the same row template as the Inbox page (extracted to an include). Every inbox action works and returns to this tab: confirm, change, transfer, pay bills, receive payment, exclude, restore, confirm ticked rows. A toggle "Show excluded" switches to Excluded rows.

**All transactions** (the register)
- Rows: every `JournalLine.objects.in_ledger()` line on this account's ledger account, newest first (date, entry id, line id, descending).
- Columns: Date; Reference # (entry reference, else the source document number); Type and counterparty (e.g. "Customer payment · Customer: Kabayan Foods"); Status ("Reconciled" when cleared in a finished reconciliation, else "Matched" when a bank row was matched to it, "From bank" when booked from a bank row, else "Manually added"); Deposits; Withdrawals; Running balance.
- Running balance is a SQL window function (`SUM(debit - credit) OVER (ORDER BY date, entry_id, id)`, sign flipped for liability accounts) over all of the account's in-ledger lines, then the page's rows are picked from it, so filters never change a row's balance. Each row's balance equals the ledger balance on the account as of that row; the newest row equals "Amount in books".
- Voided entries and their reversals both show; they net to zero.
- Filters: From, To, Type (the source labels in `ledger/sources.py`, plus "Manual entry"), Search (entry memo, reference, source number, line description; an amount like "125.00" also matches debit or credit exactly), Reconciled (Any, Reconciled, Not reconciled).
- 50 rows a page. Each row links to its source document (via `ledger.sources.describe`), or the journal entry when there is none.
- Download CSV and PDF of the filtered rows (all pages).

### 4.4 Bank register report
URL `/reports/bank-register/?account=<pk>`, slug `bank-register`, Accountant group, title "Bank register", description "Every posted line on one bank or card account, with a running balance." Same query, filters, and columns as the All transactions tab, in the report layout; it is what the tab's CSV and PDF links call.

### 4.5 Links
- Bank accounts list: each account name opens its page.
- Chart of accounts: a ledger account that has a bank account shows a "Bank page" link beside "Ledger".
- Reports index: "Bank register".

## Out of scope
New posting paths, editing bank transactions, multi-currency, any change to how inbox actions post.

## Performance
The real database has about 9,700 invoices and years of bank history. Unpaid filters use the SQL gap prefilter. The register uses one window query per page plus bulk lookups for counterparties; no per-row queries. The dashboard chart uses two aggregate queries.

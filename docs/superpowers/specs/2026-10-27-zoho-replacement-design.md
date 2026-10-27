# DNB Books: Zoho Books + Zoho Inventory Replacement

- **Date:** 2026-10-27
- **Status:** Draft for review
- **Scope of this document:** overall architecture for the full system, plus detailed scope for Phase 1 (ledger core). Phases 2 to 5 each get their own spec before they are built.

## 1. Goal

Replace Zoho Books and Zoho Inventory with a self-hosted system so the business can stop paying for the Zoho subscription, without losing accuracy, history, or audit trail.

### Success criteria

1. Every month of imported Zoho history produces a trial balance identical to Zoho's for that month (zero difference, to the cent).
2. AR aging, AP aging, and stock on hand per item match Zoho at the cutover date.
3. One real month-end close is performed in both systems in parallel with identical trial balances.
4. A backup restore drill succeeds (restored copy produces the same trial balance as production).
5. Only after 1 to 4 pass is the Zoho subscription canceled.

## 2. Context and constraints

| Topic | Decision | Source |
|---|---|---|
| Features in use | Sales, purchasing, inventory, banking, reporting | User |
| Integrations | Bank feeds only (via Plaid; user has a Plaid developer account) | User |
| Users | A few people | User |
| Hosting | Local, on one always-on office PC; others connect by browser over the office network | User |
| Maintainer | User directs, Claude writes code; user is not a developer | User |
| Inventory | One location, quantity-tracked items; no serial/batch, no kits/assemblies | User |
| Sales tax | None collected | User |
| Migration | Full transaction history from Zoho | User |
| Checks | Print on existing pre-printed voucher stock (check on top, two stubs), signed by hand | User |
| Currency | Single currency, USD | Assumption, confirmed |
| Accounting basis | Accrual; cash-basis P&L available as a report | Assumption, confirmed |
| Fiscal year | Calendar year | Assumption, confirmed |

### Out of scope (can be added later without redesign)

Multi-currency, multiple warehouses, serial/batch tracking, kits/assemblies, sales tax, e-commerce sync, customer portal, emailing documents from the app (PDFs are downloaded and attached manually), printed signatures on checks, payroll.

## 3. Architecture

> **Update 2026-10-27:** hosting moved from an office PC to **Docker on a Linux server** (`docker-compose.yml`: `app` and `scheduler` services, data in `./data`). Off-site backups go to Google Drive through rclone (`DNB_BACKUP_REMOTE`), and the monthly restore drill checks the Google Drive copy. The diagram below shows the original plan; the README has the current setup.

```
   Office PC (always on)                     Other office computers
  ┌───────────────────────────────┐          ┌──────────────┐
  │  Python web app (Django)      │◄─────────│  web browser │
  │  SQLite database (one file,   │   LAN    └──────────────┘
  │  WAL mode)                    │
  └──────────────┬────────────────┘
                 │ nightly encrypted snapshot
                 ▼
        Off-site cloud storage
```

- **Language/framework:** Python, Django. Django's admin provides working screens for every record early; custom screens are added where the workflow needs them (invoice entry, check writer, reconciliation).
- **Database:** SQLite in WAL mode. Suitable for a few concurrent users and makes backup/restore a single-file operation.
- **Money:** `Decimal` everywhere, stored with 2 decimal places for amounts. Unit costs may carry more precision (4 places) and are rounded only when posted to the ledger. No floating point.
- **Install/update:** one command to start, one command to update (exact mechanism decided in the Phase 1 plan). The user is not expected to manage dependencies by hand.
- **Access:** login required. Two roles: **admin** and **standard**. Only admins can unlock closed periods, manage users, and change check layouts. Intended for office network only; not exposed to the internet.

## 4. Modules

Each module is a Django app with one job. Documents post to the ledger through a single posting service; the ledger has no knowledge of documents beyond a reference back to the source.

```
 contacts ─┐
 items ────┤                                   ┌── banking
 sales ────┼──► posting service ──► ledger ◄───┤
 purchasing┘                                   └── (reconciliation, checks)
 side modules: migration · backup · audit log
```

| Module | Responsibility |
|---|---|
| `ledger` | Chart of accounts, journal entries and lines, accounting periods and locks, reports (trial balance, P&L, balance sheet, general ledger, cash-basis P&L) |
| `contacts` | Customers and vendors, addresses, terms, resale/exemption certificate number on customers |
| `items` | Item catalog, stock movements, cost layers, per-item costing method (FIFO or weighted average) |
| `sales` | Quotes, sales orders, invoices, customer payments, credit notes, PDF invoices |
| `purchasing` | Purchase orders, receipts, bills, vendor payments, vendor credits, PDF purchase orders |
| `banking` | Plaid feed, file import, bank feed inbox and matching rules, check writer, check layouts, reconciliation |
| `migration` | Repeatable Zoho importer and verifier |
| `backup` | Nightly encrypted off-site backup, restore drill |
| `audit` | Who changed what and when, for every record |

## 5. Ledger rules (apply to every module)

1. **Balanced entries, enforced in the posting service.** An entry whose debits do not equal its credits is rejected. Enforcement does not depend on the UI.
2. **Posted entries are immutable.** Corrections are made by voiding (which creates a reversing entry) or by a new adjusting entry. The original remains visible.
3. **Period locks.** A closed month rejects new postings. Unlocking requires an admin and is logged.
4. **Documents drive the ledger.** Invoices, bills, payments, receipts, adjustments, and checks generate their own entries. Manual journal entries exist for true adjustments only.
5. **Reports read only from the ledger.** No report computes from documents directly.
6. **Atomic posting.** A document and its journal entry are saved in one database transaction. If any part fails, nothing is saved and the user sees a plain-language error.

### Posting rules match Zoho

Specific posting behavior (for example, whether inventory asset is recorded at receipt or at bill, and how credit notes reverse cost of goods sold) will be read from the exported Zoho journals and documented in the relevant phase's spec. This keeps migrated history and new transactions consistent.

## 6. Inventory

- Stock is a list of movements (in/out with quantity and cost), never a single editable number. Stock on hand and inventory value are always derived from movements.
- Costing method is set **per item**, FIFO or weighted average, matching the method Zoho used for that item (Zoho supports both and assigns per item; items with transactions before the per-item feature were reportedly defaulted to FIFO). The importer reads the method from the export rather than assuming.
- Invariant: total inventory value from movements equals the Inventory Asset account balance.

## 7. Banking

### Feeds
- **Plaid** (`/transactions/sync`) for linked business accounts. Plaid access tokens are stored encrypted. The user should confirm which Plaid plan the account is on; a Plaid changelog dated April 16, 2026 describes a Trial plan with free Production access for up to 10 Items for new teams.
- **OFX/QFX/CSV file import** as a fallback when a bank connection breaks.
- Imported transactions land in a **bank feed inbox**. Each one is either matched to an existing record (customer payment, bill payment, printed check, deposit) or booked as a new transaction, optionally using saved rules (for example, description contains "ADOBE" goes to Software expense). Duplicate detection prevents the same bank transaction from being booked twice.

### Check writer
- Write checks to pay one or more bills, or write a direct check to an expense account.
- Check numbers are sequential per bank account. Voided or spoiled checks keep their number with VOID status; numbers are never reused. Reprints are logged.
- Signature line is left blank (signed by hand).

### Check layout designer
- Default layout: **voucher, check on top, two stubs below**, one check per page.
- Per field (date, payee name, payee address, numeric amount, amount in words, memo, stub details): position (X/Y), font size, and on/off. Saved as a named layout per bank account. Admin only.
- **Calibration print** on plain paper with adjustable global X/Y offset so the output can be aligned against real check stock.
- Assumes pre-printed stock includes the bank's MICR line; the app does not print MICR.
- Output is a PDF printed at 100% scale (no "fit to page").

### Reconciliation
- Enter statement end date and ending balance, tick cleared items (Plaid matches pre-tick likely items).
- Cannot finish until the difference is $0.00.
- Finishing locks the reconciled items and saves a PDF report listing outstanding checks and deposits in transit.
- Undoing a reconciliation is admin only and logged.

## 8. Migration from Zoho

- Source: Zoho Books **Data Backup** (Settings, Data Backup, full CSV export). Zoho limits this to once per 15 days, so one backup is taken early to build and test the importer and a final one at cutover. Per-module exports fill gaps if needed.
- The importer is **repeatable**: wipe and re-run until verification passes.
- Historical transactions are imported with the amounts **as Zoho posted them**, not recalculated.
- The **verifier** compares, for every month: trial balance by account, AR and AP aging totals, and stock quantity and value per item. Output is a difference report; the target is zero rows.
- Zoho exports and PDFs are archived alongside the backups as a permanent reference.

## 9. Backup and recovery

> **Update 2026-10-27:** backups are uploaded with rclone to Google Drive and scheduled by the `scheduler` service (01:15 daily, restore drill 02:15 on the 1st). A failed upload marks the backup failed on the home screen.

- Nightly consistent snapshot of the SQLite file (SQLite online backup API, not a raw file copy), encrypted, uploaded off-site (destination chosen in the Phase 1 plan; candidates are Google Drive or an S3-compatible bucket).
- Retention: 30 daily copies plus one per month kept long term.
- **Restore drill** command: restores the latest backup to a scratch location and confirms its trial balance matches production. Run monthly.
- Backup failures are visible on the app's home screen.

## 10. Testing

| Layer | What it proves |
|---|---|
| Unit tests on posting rules | Each document type produces the correct debits and credits |
| Invariant checks (run in tests and on demand) | Ledger balances; inventory value equals Inventory Asset account; bank register equals reconciled balance plus outstanding items |
| Migration verifier | Month-by-month match against Zoho |
| Parallel month | One live month-end close in both systems with identical results |

Development follows test-driven development. Finance skills (`finance:journal-entry`, `finance:financial-statements`, `finance:reconciliation`, `finance:close-management`) are used to review accounting logic in the relevant phases.

## 11. Build order

```
 P1 Ledger core ─► P2 Items+Inventory ─► P3 Sales ─► P4 Purchasing ─► P5 Banking+Checks+Migration ─► Parallel month ─► Cancel Zoho
```

Each phase gets its own spec, implementation plan, and working software.

## 12. Phase 1 detail: Ledger core

### Included
- Project skeleton: Django project, SQLite in WAL mode, login, admin and standard roles, start and update commands.
- Chart of accounts: account types (asset, liability, equity, income, expense, plus subtypes needed for reports such as bank, accounts receivable, accounts payable, inventory, cost of goods sold), account numbers, active/inactive.
- Journal entries: header (date, reference, memo, source type and id, status) and lines (account, debit, credit, description). Draft and posted states; void creates a reversing entry.
- Posting service: the single entry point for creating posted entries; enforces balance, period locks, and atomicity.
- Accounting periods: monthly, open/closed, admin-only unlock with audit log.
- Reports: trial balance (as of date), P&L (date range, accrual), balance sheet (as of date), general ledger detail (account, date range). Screen and CSV export.
- Audit log for all ledger records.
- Backup module: nightly encrypted off-site snapshot and restore drill (built in Phase 1 so data is protected from day one).

### Not in Phase 1
Cash-basis P&L (needs payment data from Phases 3 and 4), all document modules, banking, migration.

### Phase 1 done when
- Tests cover balance enforcement, immutability, void/reversal, period locks, and each report.
- A manual journal entry posted through the UI appears correctly on all four reports.
- A backup runs, uploads, and the restore drill passes.

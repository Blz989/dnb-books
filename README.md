# DNB Books

Accounting and inventory system for DNB, built to replace Zoho Books and Zoho Inventory. It runs in Docker on one Linux server, and everyone uses it from a web browser on the office network.

> **Status:** Order profitability and handling is built and tested, on top of Phase 5a (banking), Phase 4 (purchasing), Phase 3 (sales), Phase 2 (items and inventory), and Phase 1 (ledger core). The Zoho import (Phase 5b) is next. Not yet in use. Zoho remains the system of record until the cutover checks in [Roadmap](#roadmap) pass.

## What it does

| Area | Covers | Phase |
|---|---|---|
| Ledger | Chart of accounts, journal entries, month close, trial balance, P&L, balance sheet, general ledger | 1 |
| Backups | Nightly encrypted off-site backup, monthly restore drill | 1 |
| Items and inventory | Item catalog, stock movements, FIFO or weighted average cost per item | 2 |
| Sales | Customers, sales orders, invoices per shipment (PDF, drop-ship lines, editable), payments, deposits, credit notes, refunds, A/R aging | 3 |
| Purchasing | Vendors, purchase orders (including drop-ship POs), bills that receive stock at the bill price, vendor payments by check, ACH, or company card, vendor credits and returns, A/P aging, 1099 summary | 4 |
| Banking | Plaid bank feeds or OFX, QFX, and CSV files, an inbox that suggests matches for you to confirm, transfers and loan payments, reconciliation that locks what it clears, check printing on existing voucher stock | 5a |
| Order profitability | Profit per order (sales less stock and drop-ship cost), a report with flags, and a profit panel on each order and direct invoice | After 5a |
| Handling | Twice-monthly bill to the handling company for 55% of each order's profit not yet billed, with a statement PDF, corrections in the next bill, and a month close check | After 5a |
| Migration | Full Zoho history import with month-by-month verification | 5b |

**Not included:** multi-currency, sales tax, multiple warehouses, serial or batch tracking, kits, e-commerce sync, emailing from the app, payroll. See the spec for the full list.

### Items and inventory (Phase 2)

| Task | Where |
|---|---|
| Set up inventory accounts (once, admin) | Settings, then Items |
| Load items and opening stock from a CSV (admin) | Items, Import CSV. The preview lists every bad row. Nothing saves until all rows are valid |
| Add, edit, and look up items | Items |
| Fix a quantity | Adjustments, New adjustment. Stock can go below zero after you confirm; the cost is estimated until stock is received or counted |
| Count the shelves | Counts. Enter counts without seeing expected quantities, review the differences, then post |
| Check stock value against the books | Reports, Inventory valuation. The difference must be 0.00 |

### Sales (Phase 3)

| Task | Where |
|---|---|
| Set up sales accounts, company details, logo, default invoice notes, and the next document numbers (once, admin) | Sales settings. Set the next numbers to carry on from Zoho's |
| Add a customer | Customers, New customer. Terms default to Net 30 |
| Take an order, then invoice it a shipment at a time | Sales orders, New sales order, then Invoice a shipment. Lower a quantity or set it to 0 to leave it for later |
| Sell something the manufacturer ships straight to the customer | Mark the line "Drop ship" and name the vendor. No stock moves. It shows on the Drop shipments awaiting bill report until the vendor bill is entered (Phase 4) |
| Fix a saved invoice | Invoices, open it, Edit. The number stays, the books are reposted, and the old version stays under Revisions |
| Print or save an invoice | Invoices, open it, Download PDF |
| Record a payment | Customers or Payments, Receive payment. Checks and cash wait in Undeposited Funds; ACH and wire go straight to the bank |
| Take checks to the bank | Deposits, New deposit. Tick the payments going in this deposit |
| Give a credit or money back | Credit notes, New credit note, then Apply to an invoice. A refund is admin only, from a payment or credit with an unapplied amount |
| Void a sales document | Admin only, with a reason. Voided documents keep their numbers |
| Check A/R against the books | Reports, A/R aging and Undeposited funds. Both show a Difference that must be 0.00 |

### Purchasing (Phase 4)

| Task | Where |
|---|---|
| Set up the accounts payable account, the next document numbers, and the 1099 thresholds (once, admin) | Purchasing settings. Set the next numbers to carry on from Zoho's |
| Add a vendor, mark 1099 vendors, and enter tax IDs | Vendors, New vendor. Only an admin sees a full tax ID, and only on the edit form |
| Track what was ordered | Purchase orders, New purchase order. Nothing is sent to the vendor |
| Receive and bill stock | Open the purchase order, then Bill this order. Stock comes in at the price on the bill. Bill in parts by lowering a quantity, and add freight as an expense line |
| Enter a bill with no purchase order, or an expense bill (rent, utilities) | Bills, New bill. Item lines and expense lines can share a bill |
| Buy something that ships straight to the customer | Open the sales order, then Create drop-ship PO. Billing it posts the cost and clears the invoice from Drop shipments awaiting bill |
| Fix a saved bill | Bills, open it, Edit. The number stays, the books are reposted, and the old version stays under Revisions. Voids are admin only |
| Pay bills | Pay bills. Tick amounts (partial is fine), choose check, ACH, or company card. One payment is made for each vendor, and each check needs its own number |
| Pay a vendor ahead, or without a bill | Pay bills, Single payment. Anything not applied stays as credit with the vendor |
| Send stock back, or get a credit with no return | Vendor credits, Return to vendor or Credit without a return, then Apply to a bill |
| Check A/P against the books | Reports, A/P aging. The Difference must be 0.00 |
| Year-end 1099 totals | Reports, 1099 summary. Check and ACH payments only. Admins can download the CSV for the accountant. Confirm amounts and thresholds with the accountant before filing |

### Banking (Phase 5a)

Nothing is booked from the bank feed until you confirm it. Confirming a row that matches a document already in the books only links the two.

| Task | Where |
|---|---|
| Add a bank, card, or loan account (once, admin) | Bank accounts, Add account. Pick the ledger account and its kind. Set the next check number for a checking account |
| Bring in a statement | Bank accounts, Import file. OFX and QFX files read themselves. The first CSV for an account asks which column is which and remembers it. Rows already imported are skipped |
| Bring in transactions automatically (admin) | Bank accounts, Link with Plaid. The bank's own sign-in opens in a pop-up. It syncs four times a day, or press Sync now |
| Work the inbox | Inbox. Confirm a suggestion, Change it to another match or an account, make it a Transfer, or Exclude it with a reason. Tick rows and Confirm ticked rows to do several at once |
| Book the same fee every month | Inbox, Rules. The first active rule that matches the description suggests its account |
| Move money between your accounts, pay the card, pay the loan | Transfers, New transfer, or Transfer on an inbox row. Make it once from either side. The other account's row matches the same transfer and books nothing new |
| Reconcile an account to its statement | Reconcile. Enter the statement date and ending balance (for a card or loan, the amount owed as a positive number), tick what is on the statement, and finish at 0.00 difference. Only an admin can finish or undo it |
| Print a vendor or refund check | Pay by check and leave the number blank, then Checks to print, Print check. Print at 100%, then answer whether it printed correctly. A spoiled number is never used again |
| Fit the printing to your check stock (admin) | Checks to print, open a check, or Bank accounts. Use the layout page: Print calibration page, hold it against a check in front of a light, nudge the offsets, and print it again |

A reconciled month is locked: the documents behind it can't be voided or edited until an admin undoes that reconciliation (the most recent one only).

A month won't close while customer balances, undeposited payments, or vendor balances disagree with the ledger. An admin can close it anyway from Admin, Accounting periods, with a reason that is saved on the period.

### Order profitability and handling

Each order's profit is its sales, less credits applied to its invoices, less the cost of what shipped on it (FIFO stock cost plus drop-ship bill cost). An invoice with no sales order is its own order. The handling company is paid 55% of each order's profit, never below zero.

| Task | Where |
|---|---|
| Set up handling (once, admin) | Purchasing settings, Edit handling settings. Pick the handling vendor and the Handling expense account, set the start date, and confirm the 55% rate. Orders dated before the start date never get handling |
| Change the rate (admin) | Handling settings, Add rate. It applies to orders dated on or after its date |
| See the profit of every order | Reports, Order profitability. Filter by date, customer, or "Has unbilled handling". Flags show a drop-ship line whose bill isn't entered, an order below zero, and orders before the start date |
| See one order's profit | Open the sales order or direct invoice. The profit panel shows sales by invoice, credits, cost by line, and each handling bill that billed it |
| Bill handling, on the 15th and the last day of the month | Handling, Next. Check the preview, then Create bill. The bill carries each order's handling to date less what earlier bills carried, so a late drop-ship bill, a credit, or an edit is corrected in the next bill. If corrections exceed new handling, it creates a vendor credit instead |
| Pay the handling company | Pay bills, like any other bill |
| Undo the latest handling bill (admin) | Handling, open the run, Void. Only the latest run can be voided, and only before it is paid. Handling bills can't be voided or edited from Bills |

A month won't close until both of its handling bills are created. An admin can close it anyway with a reason.

## How it works

```
   Linux server (Docker)                                Office computers
  ┌────────────────────── docker compose ──────────────┐   ┌──────────────┐
  │  app        DNB Books (Python / Django) on :8000   │◄──│  web browser │
  │  scheduler  nightly backup, restore drill, bank sync│  └──────────────┘
  │  ./data     database (one SQLite file), backups    │
  └──────────────────────┬─────────────────────────────┘
                         │ nightly encrypted copy (rclone)
                         ▼
                 Google Drive (dnb-backups folder)
```

Rules the system enforces:

- Every entry balances (debits equal credits), or it is not saved.
- Amounts are exact to the cent. No floating point.
- Posted entries cannot be edited. Mistakes are fixed by voiding, which creates a reversing entry and keeps the original on record.
- Closed months are locked. Only an admin can reopen one, and must give a reason.
- Reports read only from the ledger, so they always agree with each other.
- Every change records who made it and when.

## Getting started

### First-time install (on the server)

You need Docker with the Compose plugin, and git.

1. Get the code: `git clone <repository URL> dnb-books && cd dnb-books`
2. Copy `.env.example` to `.env` and fill in:
   - `DNB_SECRET_KEY`: any long random text (for example the output of `openssl rand -hex 32`).
   - `DNB_ALLOWED_HOSTS`: the server's name and IP address as people will type them, plus `localhost`.
   - `DNB_TIME_ZONE`: your time zone, for example `America/Los_Angeles`. Backup times use it.
3. Build and start: `docker compose up -d --build`. The first start creates the database in `./data`.
4. Create the backup key and the first admin user:

```bash
docker compose exec -u dnb app python manage.py generate_backup_key
docker compose exec -u dnb app python manage.py createsuperuser
```

   Put the printed key in `.env` as `DNB_BACKUP_KEY`, save a copy in your password manager, then `docker compose up -d` so the app reads it.
5. Connect Google Drive for off-site backups (next section).
6. Open `http://<server>:8000` and log in.

Always run commands inside the container with `-u dnb`, as shown, so files in `./data` stay owned by the app.

### Connect Google Drive (once)

Backups are encrypted, then copied to Google Drive with [rclone](https://rclone.org/drive/).

1. On the server, start rclone's setup: `docker compose run --rm -u dnb app rclone config`
2. Choose `n` (new remote), name it `gdrive`, storage type `drive`. Leave client id and secret blank, choose scope `1` (full access), and leave the rest at their defaults.
3. When it asks "Use web browser to automatically authenticate?", answer `n`. The server has no browser, so rclone prints a command like `rclone authorize "drive" "..."`.
4. On any computer with a browser, [install rclone](https://rclone.org/install/), run that exact command, sign in to the Google account that should hold the backups, and paste the token it prints back into the server prompt.
5. Finish the prompts, then set `DNB_BACKUP_REMOTE=gdrive:dnb-backups` in `.env` and run `docker compose up -d`.
6. Test it: `docker compose exec -u dnb app python manage.py backup_now`, then check that the file appears in the `dnb-backups` folder in Google Drive, and run `docker compose exec -u dnb app python manage.py restore_drill`.

### Banking setup (Phase 5a)

Do these once after `./update.sh`.

1. **Make the key that protects bank sign-ins.** Run `docker compose exec -u dnb app python manage.py generate_field_key`, put the printed key in `.env` as `DNB_FIELD_KEY`, and save a copy in your password manager. Without it, linked banks can't sync and have to be linked again. Then `docker compose up -d`.
2. **Plaid keys (optional).** From your Plaid developer account, put `PLAID_CLIENT_ID` and `PLAID_SECRET` in `.env`, and `PLAID_ENV=sandbox` or `production`. Production needs Plaid's approval for live banks. Without keys, the Link button is hidden and file import still works. Sync times default to 06:00, 10:00, 14:00, and 18:00 (`DNB_BANK_SYNC_TIMES`).
3. **Try Plaid safely (with Sandbox keys only).** `docker compose exec -u dnb app python manage.py plaid_sandbox_check` links Plaid's test bank, syncs it, and prints the counts. It refuses to run against production.
4. **Add the bank accounts** under Banking, Bank accounts, then link or import. Set each checking account's next check number.
5. **Calibrate the check printing.** Open the layout page for the account, print the calibration page at 100% on plain paper, hold it against a voucher check in front of a light, and nudge the offsets until the boxes sit on the lines. Then print one real check on plain paper held against the stock before using the stock.

### Import from Zoho (Phase 5b, once)

The import replays the whole Zoho history into a new, empty database. It never writes to the live database file directly: it copies the database to `dnb.sqlite3.import-copy`, imports into the copy, and compares the result with Zoho's own reports (the tie-out). A real run replaces the database with the copy only when the tie-out passes, and only after a backup. If anything goes wrong, the copy is deleted and the database is left as it was. Expect it to take tens of minutes.

1. Put the Zoho export zip in `./data/zoho/` (for example `./data/zoho/export.zip`) and Zoho's reports in `./data/zoho/reports/`: `trial_balance.csv`, `pl_2020.csv` to `pl_2026.csv`, `ar_aging.csv`, `ap_aging.csv`, `inventory_summary.csv`, and `inventory_valuation.csv`. Optional: `opening_balances.csv` and `deposit_pairs.csv`.
2. Stop the app and the scheduler, so nothing writes to the database while it is swapped:

```bash
docker compose stop app scheduler
```

3. Dry run. It prints progress per month (`2021-03: 412 events`), writes `report.html` and `report.pdf` next to the export, and keeps nothing else. It exits 0 when the tie-out passes and 1 when it doesn't.

```bash
docker compose run --rm -u dnb app python manage.py import_zoho /data/zoho/export.zip --reports /data/zoho/reports --user <admin username> --dry-run
```

   Add `--opening-balances /data/zoho/opening_balances.csv` and `--deposit-pairs /data/zoho/deposit_pairs.csv` when you have them. A transfer out of Undeposited Funds with no exact set of payments stops the run; list its payments in `deposit_pairs.csv` (`date,amount,payment_numbers`, numbers separated by `;`) and run again.
4. Read the report. When it is clean, run the same command without `--dry-run`. It backs up the database, swaps in the import, and prints `Imported. Restart the app.` To import even though some differences remain, add `--accept-differences`.
5. Start the app again: `docker compose up -d`.

The command refuses a database that already has documents, and lists every problem in the export (one per line) before anything is imported.

### Everyday use

| Task | How |
|---|---|
| Open the app | `http://<server>:8000` |
| Update the app | `./update.sh`: backs up, pulls the new version, rebuilds, and restarts. Database changes apply on start. |
| Back up now | `docker compose exec -u dnb app python manage.py backup_now` |
| Check backups work | `docker compose exec -u dnb app python manage.py restore_drill` |
| See what the scheduler did | `docker compose logs scheduler` |
| Stop / start | `docker compose down` / `docker compose up -d` |

### Backups

The `scheduler` service runs `backup_now` every day at 1:15 AM, `restore_drill` on the 1st of each month at 2:15 AM, and `sync_banks` at the times in `DNB_BANK_SYNC_TIMES` (server time zone; change with `DNB_BACKUP_TIME` and `DNB_DRILL_TIME`). Each backup is encrypted, written to `./data/backups`, and copied to Google Drive. The app keeps the last 30 days plus the newest backup of every month, in both places. The restore drill downloads the backup from Google Drive, so it proves the off-site copy works.

If a backup or upload fails, the home screen shows a red warning, for example "Backup saved locally but not uploaded: …".

**Restore**

```bash
# 1. Copy the backup down from Google Drive (skip if it is still in ./data/backups)
docker compose run --rm -u dnb app rclone copy gdrive:dnb-backups/<backup file> /data/backups/
# 2. Decrypt it to a new file (never overwrites)
docker compose run --rm -u dnb app python manage.py restore_backup /data/backups/<backup file> --to /data/restored.sqlite3
# 3. Swap it in
docker compose down
mv data/dnb.sqlite3 data/dnb-before-restore.sqlite3 && rm -f data/dnb.sqlite3-wal data/dnb.sqlite3-shm
mv data/restored.sqlite3 data/dnb.sqlite3
docker compose up -d
```

**Important:** backups are encrypted. Keep a copy of the backup key (`DNB_BACKUP_KEY` in `.env`) in a password manager. Without it, backups cannot be restored. The rclone sign-in lives in `./data/rclone/rclone.conf`; treat it like a password.

**Network:** the app serves plain HTTP and is meant for the office network only. Do not expose port 8000 to the internet without putting a reverse proxy with HTTPS in front of it.

## Roadmap

```
 P1 Ledger core ─► P2 Items+Inventory ─► P3 Sales ─► P4 Purchasing ─► P5a Banking+Checks ─► P5b Zoho migration ─► Parallel month ─► Cancel Zoho
```

Zoho is canceled only after all of these pass:

1. Every month of imported history matches Zoho's trial balance to the cent.
2. Customer and vendor balances and stock on hand match Zoho at cutover.
3. One real month-end close gives identical results in both systems.
4. A backup restore drill succeeds.

## Documentation

- Design spec: [`docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`](docs/superpowers/specs/2026-10-27-zoho-replacement-design.md)
- Phase plans: [`docs/superpowers/plans/`](docs/superpowers/plans/)
- Phase specs: [`docs/superpowers/specs/`](docs/superpowers/specs/)
- Instructions for Claude Code sessions: [`CLAUDE.md`](CLAUDE.md)

## Development

```bash
uv sync            # install dependencies
uv run pytest -q   # run tests
./start.sh         # run the app outside Docker (uses .env)
```

Each phase gets its own spec and plan before any code is written. Work happens on `claude/*` branches and lands through pull requests into `main`.

### Taking screenshots
Screens are checked visually against `docs/design/items-mockup.html`. Use a throwaway database, never `data/`:

```bash
export DNB_DB_PATH=/tmp/demo.sqlite3 DNB_DEBUG=1
uv run python manage.py migrate
uv run python manage.py seed_demo          # user demo / demo-pass-123, with items, customers, invoices, and payments; refuses a non-empty database
uv run python manage.py runserver 8765 --noreload &
node scripts/screenshots.mjs http://127.0.0.1:8765 /tmp/shots / /reports/trial-balance/
```

This saves `<name>-light.png` and `<name>-dark.png` (full page) for each path. It uses the Playwright that is pre-installed in cloud sessions and is not an app dependency.

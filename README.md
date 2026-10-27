# DNB Books

Accounting and inventory system for DNB, built to replace Zoho Books and Zoho Inventory. It runs on one computer in the office, and everyone else uses it from a web browser on the office network.

> **Status:** Phase 2 (items and inventory) is built and tested, on top of Phase 1 (ledger core). Not yet in use. Zoho remains the system of record until the cutover checks in [Roadmap](#roadmap) pass.

## What it does

| Area | Covers | Phase |
|---|---|---|
| Ledger | Chart of accounts, journal entries, month close, trial balance, P&L, balance sheet, general ledger | 1 |
| Backups | Nightly encrypted off-site backup, monthly restore drill | 1 |
| Items and inventory | Item catalog, stock movements, FIFO or weighted average cost per item | 2 |
| Sales | Customers, quotes, sales orders, invoices (PDF), payments, credit notes | 3 |
| Purchasing | Vendors, purchase orders, receiving, bills, vendor payments and credits | 4 |
| Banking | Plaid bank feeds, file import, check printing on existing check stock, reconciliation | 5 |
| Migration | Full Zoho history import with month-by-month verification | 5 |

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

## How it works

```
   Office PC (always on)                     Other office computers
  ┌───────────────────────────────┐          ┌──────────────┐
  │  DNB Books (Python / Django)  │◄─────────│  web browser │
  │  Database: one SQLite file    │   LAN    └──────────────┘
  └──────────────┬────────────────┘
                 │ nightly encrypted copy
                 ▼
   Synced cloud folder (Google Drive / OneDrive / Dropbox)
```

Rules the system enforces:

- Every entry balances (debits equal credits), or it is not saved.
- Amounts are exact to the cent. No floating point.
- Posted entries cannot be edited. Mistakes are fixed by voiding, which creates a reversing entry and keeps the original on record.
- Closed months are locked. Only an admin can reopen one, and must give a reason.
- Reports read only from the ledger, so they always agree with each other.
- Every change records who made it and when.

## Getting started

### First-time install (on the office PC)

1. Install [uv](https://docs.astral.sh/uv/) (it installs Python and the dependencies for you).
2. Copy `.env.example` to `.env` and fill in `DNB_SECRET_KEY` (any long random text), `DNB_ALLOWED_HOSTS` (the office PC's name or IP address, plus `localhost`), and `DNB_BACKUP_DIR` (a folder inside your synced cloud folder, such as Google Drive, OneDrive, or Dropbox).
3. Run these once:

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py setup_roles
uv run python manage.py generate_backup_key
```

The last command prints a backup key. Put it in `.env` as `DNB_BACKUP_KEY` and keep a copy in your password manager.

### Everyday use

| Task | How |
|---|---|
| Start the app | Run `start` (`start.bat` on Windows, `start.sh` on Linux), then open `http://<office-pc>:8000` |
| Update the app | Run `update` (`update.bat` or `update.sh`). It takes a backup first, then pulls the new version and applies database changes |
| Back up now | `uv run python manage.py backup_now` |
| Check backups work | `uv run python manage.py restore_drill` |

### Nightly backup

Backups are encrypted copies of the database written to `DNB_BACKUP_DIR`. Your sync app (Google Drive, OneDrive, Dropbox) uploads them off-site. The app keeps the last 30 days plus the newest backup of every month. If the backup folder is missing or unwritable, the home screen shows a red warning.

Schedule `backup_now` for 1:15 AM every day and `restore_drill` for the 1st of each month.

**Windows (Task Scheduler)**

1. Open Task Scheduler and choose Create Task.
2. Trigger: Daily at 1:15 AM. Action: Start a program.
   - Program: `uv`
   - Arguments: `run python manage.py backup_now`
   - Start in: the folder where DNB Books is installed (for example `C:\dnb-books`)
3. Make a second task the same way with a Monthly trigger on day 1 at 2:00 AM and the arguments `run python manage.py restore_drill`.
4. The next morning, check that the home screen shows "Last backup" with a recent time.

**Linux (cron)**: run `crontab -e` and add (use the real paths):

```
15 1 * * * cd /home/office/dnb-books && uv run python manage.py backup_now >> data/backup.log 2>&1
0 2 1 * * cd /home/office/dnb-books && uv run python manage.py restore_drill >> data/backup.log 2>&1
```

**Restore**

```bash
uv run python manage.py restore_backup <backup file> --to <new database file>
```

The command never overwrites a file. It prints the steps to swap the restored database in: stop the app, rename the current database, move the restored file to `DNB_DB_PATH`, start the app.

**Important:** backups are encrypted. Keep a copy of the backup key (`DNB_BACKUP_KEY` in `.env`) in a password manager. Without it, backups cannot be restored.

## Roadmap

```
 P1 Ledger core ─► P2 Items+Inventory ─► P3 Sales ─► P4 Purchasing ─► P5 Banking+Checks+Migration ─► Parallel month ─► Cancel Zoho
```

Zoho is canceled only after all of these pass:

1. Every month of imported history matches Zoho's trial balance to the cent.
2. Customer and vendor balances and stock on hand match Zoho at cutover.
3. One real month-end close gives identical results in both systems.
4. A backup restore drill succeeds.

## Documentation

- Design spec: [`docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`](docs/superpowers/specs/2026-10-27-zoho-replacement-design.md)
- Phase 1 plan: [`docs/superpowers/plans/2026-10-27-phase-1-ledger-core.md`](docs/superpowers/plans/2026-10-27-phase-1-ledger-core.md)
- Instructions for Claude Code sessions: [`CLAUDE.md`](CLAUDE.md)

## Development

```bash
uv sync            # install dependencies
uv run pytest -q   # run tests
```

Each phase gets its own spec and plan before any code is written. Work happens on `claude/*` branches and lands through pull requests into `main`.


## Development

### Taking screenshots

Screens are checked visually against `docs/design/items-mockup.html`. Use a throwaway database, never `data/`:

```bash
export DNB_DB_PATH=/tmp/demo.sqlite3 DNB_DEBUG=1
uv run python manage.py migrate
uv run python manage.py seed_demo          # user demo / demo-pass-123; refuses a non-empty database
uv run python manage.py runserver 8765 --noreload &
node scripts/screenshots.mjs http://127.0.0.1:8765 /tmp/shots / /reports/trial-balance/
```

This saves `<name>-light.png` and `<name>-dark.png` (full page) for each path. It uses the Playwright that is pre-installed in cloud sessions and is not an app dependency.

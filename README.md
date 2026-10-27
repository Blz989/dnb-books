# DNB Books

Accounting and inventory system for DNB, built to replace Zoho Books and Zoho Inventory. It runs in Docker on one Linux server, and everyone uses it from a web browser on the office network.

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
   Linux server (Docker)                                Office computers
  ┌────────────────────── docker compose ──────────────┐   ┌──────────────┐
  │  app        DNB Books (Python / Django) on :8000   │◄──│  web browser │
  │  scheduler  nightly backup, monthly restore drill  │   └──────────────┘
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

The `scheduler` service runs `backup_now` every day at 1:15 AM and `restore_drill` on the 1st of each month at 2:15 AM (server time zone; change with `DNB_BACKUP_TIME` and `DNB_DRILL_TIME`). Each backup is encrypted, written to `./data/backups`, and copied to Google Drive. The app keeps the last 30 days plus the newest backup of every month, in both places. The restore drill downloads the backup from Google Drive, so it proves the off-site copy works.

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
 P1 Ledger core ─► P2 Items+Inventory ─► P3 Sales ─► P4 Purchasing ─► P5 Banking+Checks+Migration ─► Parallel month ─► Cancel Zoho
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
uv run python manage.py seed_demo          # user demo / demo-pass-123; refuses a non-empty database
uv run python manage.py runserver 8765 --noreload &
node scripts/screenshots.mjs http://127.0.0.1:8765 /tmp/shots / /reports/trial-balance/
```

This saves `<name>-light.png` and `<name>-dark.png` (full page) for each path. It uses the Playwright that is pre-installed in cloud sessions and is not an app dependency.

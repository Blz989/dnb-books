# Sidebar, FIFO lots, unpaid invoices, and bank account page: implementation plan

Spec: `docs/superpowers/specs/2026-11-04-reports-and-bank-page-design.md` (binding).
PR title: "Sidebar width, FIFO lot tracking, unpaid invoices, and bank account page"
Branch: `claude/clever-brown-smuekv`

## Tech Stack
Django 5.2, SQLite, WeasyPrint (existing). No new dependencies.

## Global Constraints
- Money is `Decimal`. SQLite sums decimals as floats: wrap every SQL sum in `ledger.balances.money()`.
- No new posting paths. Nothing in this plan calls `post_entry`, `post_draft`, or `void_entry` except through existing inbox views reused unchanged.
- Ledger figures read `JournalLine.objects.in_ledger()` only.
- No per-row or per-invoice query loops over unbounded sets. Use SQL aggregates, the `invoices_with_open_balance` prefilter, bulk `in_bulk`/`filter(number__in=...)` lookups.
- User-visible strings are exactly those in the spec and this plan. American English. Plain, short UI text.
- Every report: registered in `ledger/urls.py` wrapped in `pdf_export`, listed in `core/report_catalog.py` GROUPS, template extends `core/report_base.html`, CSV via `?format=csv` using `ledger.csv_export.csv_response`.
- Screens follow the existing look (`core/static/core/app.css` classes: `card`, `filters`, `data`, `kpis`/`kpi`, `banner`, `tag`, `num`, `neg`, `total`, `section`). Both light and dark themes must work; use CSS variables only, no hard-coded colors.
- Tests in `tests/`, pytest style, file names as given. TDD: write the test, see it fail, implement, see it pass.
- Never skip, xfail, or delete a failing test. Do not weaken model guards or triggers.
- One commit per task minimum with the commit message given. Tick this plan's checkboxes for the task in the same commit.
- Screenshots (owner's standing rule): for each task that changes a screen, run the app against a throwaway demo DB (`DNB_DB_PATH=<scratch>/demo.sqlite3 uv run python manage.py migrate && ... seed_demo`, then add whatever sample data the screen needs through the normal services, never raw ledger writes), start `uv run python manage.py serve` on a free port, log in as demo / demo-pass-123, and capture full-page PNGs at deviceScaleFactor 2 with the pre-installed Playwright (`require('playwright')` in Node, `chromium.launch()`), in light and dark (`localStorage dnb-theme`), plus a 390px-wide mobile shot where the task says so. Save them under the scratch directory given in the dispatch and list them in the report. Never use `data/`. Do not commit screenshots.

## Review Focus
- Unpaid report grand total == A/R aging total == A/R account balance.
- FIFO trace leaves replay results byte-for-byte unchanged when `trace` is None or given.
- Register running balance equals the ledger balance at each row regardless of filters; newest row equals "Amount in books".
- Query counts stay flat as data grows (use `django_assert_max_num_queries` where the plan says).

---

## Task 1: Sidebar width

Files: `core/static/core/app.css`, `tests/test_sidebar.py` (new).

- [x] Test: GET home as a logged-in user with company name "D&B Goods Trading Corporation" (Settings company profile model in `core/company.py`); assert the response contains the full name, and assert `app.css` contains `grid-template-columns: 264px 1fr` and that the `.brand-name` rule has no `white-space: nowrap` and has `-webkit-line-clamp: 2`.
- [x] CSS: `.app` grid column 236px → 264px. `.brand-name`: replace `overflow: hidden; text-overflow: ellipsis; white-space: nowrap;` with `white-space: normal; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere;`. Leave the 900px media query stacking as is; confirm no horizontal scroll at 390px.
- [x] Screenshots: home page light and dark at 1440px with the long company name and no logo; same at 390px wide.
- [x] Commit: `Widen the sidebar so long company names fit`

## Task 2: Unpaid invoices filter and "Unpaid invoices by customer" report

Files: `sales/reports.py`, `sales/views/invoices.py`, `sales/templates/sales/invoice_list.html`, `sales/views/reports.py`, `sales/templates/sales/report_unpaid_invoices.html` (new), `sales/csv_export.py`, `ledger/urls.py`, `core/report_catalog.py`, `tests/test_unpaid_invoices.py` (new).

Interfaces:
```python
# sales/reports.py
@dataclass
class AgedInvoice:
    invoice: Invoice
    balance: Decimal
    days_past_due: int      # (as_of - due_date).days, may be <= 0
    bucket: str             # one of BUCKETS

def aged_open_items(as_of: date, customer: Customer | None = None) -> tuple[list[AgedInvoice], dict[int, Decimal]]:
    """Every invoice with a nonzero balance on as_of (from invoices_with_open_balance + invoice_balance), and each
    customer's unapplied payments plus credit notes as a NEGATIVE amount keyed by customer id. The only source
    ar_aging and unpaid_by_customer read."""

@dataclass
class UnpaidGroup:
    customer: Customer
    invoices: list[AgedInvoice]   # sorted by due_date, date, number
    credits: Decimal              # <= 0
    totals: AgingRow              # buckets from invoices, unapplied_credits = credits

@dataclass
class UnpaidReport:
    as_of: date
    groups: list[UnpaidGroup]     # customers with a nonzero AgingRow.total, sorted by name (case-insensitive)
    totals: AgingRow
    ledger_balance: Decimal
    difference: Decimal

def unpaid_by_customer(as_of: date) -> UnpaidReport
```
`ar_aging` is rewritten to build its rows from `aged_open_items` (behavior unchanged: same rows, totals, ledger, difference). Invoices with a negative balance (overpaid) stay in buckets exactly as A/R aging has them today.

Invoice list: `?unpaid=1` → `invoices = invoices_with_open_balance(None).filter(_gap__gt=Decimal("0.005"))` then the existing status/customer/q/overdue filters; add `unpaid` to the template context and the pagination querystring. Checkbox label "Unpaid only", placed before "Overdue only".

Report view `unpaid_invoices` (AsOfForm, default today); route `reports:unpaid-invoices` at `unpaid-invoices/`; catalog entry in Receivables after "open-invoices": `("unpaid-invoices", "Unpaid invoices by customer", "Each customer's unpaid invoices, aged, for collections.")`. Page subtitle "As of YYYY-MM-DD, days past due". Columns: Invoice, Date, Due, Days past due, Current, 1-30, 31-60, 61-90, Over 90, Credits, Balance. Customer heading row uses `tr.section`; subtotal row `tr.total` labeled "Total {name}"; grand total row "Total". Empty state "Nothing is owed on this date." KPI tiles "Total unpaid", "A/R account", "Difference" and the banner "Customer balances and the A/R account don't match. Difference: X." when nonzero. CSV per spec.

Tests (`tests/test_unpaid_invoices.py`), building data with the existing sales services (see `tests/test_sales_reports.py` for fixtures/helpers):
- [x] unpaid filter lists a partly paid invoice and an unpaid not-yet-due one, excludes a fully paid one and a void one; overdue filter still works alone and combined.
- [x] unpaid filter query count does not grow with invoice count (`django_assert_max_num_queries`, compare 3 vs 30 invoices).
- [x] report groups by customer, puts each invoice in the right bucket for as_of, shows the unapplied credit line, subtotals per customer.
- [x] grand total balance == `ar_aging(as_of).totals.total` == A/R account balance from `sales.checks._account_balance(SalesSettings.load().ar_account, as_of)` on the same date, with a payment left partly unapplied and a credit note left unapplied, for two different as_of dates.
- [x] ar_aging results are unchanged by the refactor (existing `tests/test_sales_reports.py` passes untouched).
- [x] CSV header and a subtotal row; PDF returns `application/pdf`; report listed in `core.report_catalog.catalog()`.
- [x] Screenshots: invoices list with Unpaid only ticked; the report (light, dark); the report PDF first page rendered to PNG; report empty state.
- [x] Commit: `Unpaid invoices filter and unpaid invoices by customer report`

## Task 3: FIFO cost lot tracking report

Files: `items/costing.py`, `items/lots.py` (new), `items/forms.py`, `items/views.py`, `items/templates/items/fifo_lots.html` (new), `items/csv_export.py`, `items/reports.py` (per-item valuation helper), `ledger/urls.py`, `core/report_catalog.py`, `tests/test_fifo_lots.py` (new).

Interfaces:
```python
# items/costing.py  (additions only; replay's results must not change)
@dataclass(frozen=True)
class Consume: layer_id: int; outflow_id: int; quantity: int; cost: Decimal
@dataclass(frozen=True)
class Short: outflow_id: int; quantity: int; estimate: Decimal
@dataclass(frozen=True)
class Fill: inflow_id: int; outflow_id: int; quantity: int; cost: Decimal
def replay(method, moves, fallback_unit_cost, trace: list | None = None) -> ReplayResult
```
Record `Consume` inside the FIFO outflow loop (per layer slice, `cost=part`), `Short` where the backlog entry is added, `Fill` per backlog fill (`cost` = the fill cost after the last-fill adjustment). Weighted average appends nothing.

```python
# items/reports.py
def item_valuation(item: Item, as_of: date) -> tuple[int, Decimal]:
    """Same rules as inventory_valuation, for one item (refactor inventory_valuation to share the per-item sums)."""

# items/lots.py
@dataclass
class LotRow:            # one display row
    lot: "Lot"; first: bool
    out_date: date | None; out_label: str; out_url: str | None; out_party: str; out_quantity: int | None
    tag: str             # "", "No invoice yet", "Sold before received"
@dataclass
class Lot:
    movement: StockMovement; date: date; label: str; url: str | None; party: str
    quantity: int; unit_cost: Decimal; total: Decimal; age_days: int
    opening: bool; on_hand_at_start: int; used_before_start: int
    remaining_quantity: int; remaining_value: Decimal
@dataclass
class ShortRow:
    date: date; label: str; url: str | None; party: str; quantity: int; estimate: Decimal
    resolved: str        # "Filled by Bill BILL-… on YYYY-MM-DD" or "Still estimated"
@dataclass
class LotReport:
    item: Item; start: date; end: date
    lots: list[Lot]; rows: list[LotRow]; shorts: list[ShortRow]
    received: int; sold: int
    on_hand: int; value: Decimal                      # item_valuation(item, end)
    lots_quantity: int; lots_value: Decimal           # sum of remaining in the trace
    difference: Decimal                               # lots_value - value
def fifo_lots(item: Item, start: date, end: date) -> LotReport
```
Labels and links per movement `source_type`: `purchasing.bill` → "Bill {number}", `reverse("purchasing:bill-detail", args=[number])`, party vendor name; `sales.invoice` → "Invoice {number}", `sales:invoice-detail`, party customer name; `purchasing.vendor_credit` → "Vendor credit {number}", `purchasing:credit-detail`, party vendor name; `migration.*` → "Zoho import", no link, party = reason; anything else → `movement.get_kind_display()`, no link, party = reason. Look up bills, invoices, vendor credits in bulk by number (one query per document type). Movements counted: `date <= end` and not (`voided` and `voided_on <= end`). Age: days from lot date to the date its last unit was consumed if `remaining_quantity == 0` by end, else to end.

Form `FifoLotsForm(RangeForm)` with `item = forms.ModelChoiceField(queryset=Item.objects.filter(item_type=STOCK, costing_method=FIFO).order_by("sku"), required=False)` labeled "Item", option label "SKU · name". View `fifo_lots` (defaults Jan 1 → today); without an item, show the form and "Pick an item to trace its cost lots."; a non-FIFO item id in the URL → field error "Lot tracking needs an item that uses FIFO costing." Route `reports:fifo-lots` at `fifo-lots/`, catalog Inventory group after stock-movements: `("fifo-lots", "FIFO cost lot tracking", "Each receipt of one item and the sales that used it.")`.

Template: title block "FIFO cost lot tracking"; subtitle "{item name} · SKU {sku} · {start} to {end}"; KPI tiles Received, Sold, On hand at {end}, Value at {end}; main table with a two-cell group header row "PRODUCT IN" (colspan 7) / "PRODUCT OUT" (colspan 4); "Sold with no matching receipt" card (only if shorts); closing line and the mismatch banner exactly as the spec words them. CSV per spec.

Tests (`tests/test_fifo_lots.py`), building data through `record_movements` / bill and invoice posting services (see `tests/test_stock.py`, `tests/test_bills.py`, `tests/test_invoicing.py` for helpers):
- [x] property-style: for a few random move sequences (use hypothesis like `tests/test_costing.py` if it does), `replay(..., trace=[])` returns a result equal to `replay(...)`; sum of Consume quantities per outflow + Short quantities == outflow quantity.
- [x] two bills and three invoices where one invoice spans both lots and one lot feeds two invoices → rows pair correctly, quantities and costs right, links to bill and invoice.
- [x] a bill with no invoice → tag "No invoice yet"; an invoice before any receipt → listed in shorts, then filled by a later bill → "Filled by Bill …" and the lot row tagged "Sold before received"; unfilled → "Still estimated".
- [x] opening lot: received before From with units left → "Opening", on-hand-at-start and used-before-start right.
- [x] closing on hand and value equal `inventory_valuation(end)` row for the item; `difference == 0`; lots remaining match `FifoLayer` rows when end is today.
- [x] voided invoice excluded; item required; non-FIFO error message; CSV header; PDF content type; catalog entry.
- [x] query count independent of the number of invoices (bulk lookups).
- [x] Screenshots: report with gaps (light, dark), no-item empty state, non-FIFO error, PDF first page.
- [x] Commit: `FIFO cost lot tracking report`

## Task 4: Bank-reported balance from feeds

Files: `banking/models.py`, new migration, `banking/plaid_client.py`, `banking/sync.py`, `banking/importers.py`, `banking/views/accounts.py`, `tests/fixtures/plaid/` (new fixture page with `accounts`), `tests/test_bank_balance.py` (new).

- `BankAccount.bank_balance = DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)`, `bank_balance_on = DateField(null=True, blank=True)`. Not editable in `BankAccountForm`.
- `SyncPage` gains `balances: dict[str, Decimal]` (Plaid account_id → `balances.current` as Decimal via `str()`), default empty; `parse_sync_response` fills it from `payload.get("accounts", [])` skipping null `current`.
- `sync_item`: after applying pages, for each mapped account whose id appears in the last page carrying balances, set `bank_balance` and `bank_balance_on = timezone.localdate()`.
- `importers.parse_ofx_balance(data: bytes) -> tuple[Decimal, date] | None` reads the first statement's `ledgerbal` (`balamt`, `dtasof.date()`), None if missing. `import_file` OFX branch stores it, negated when `account.is_liability`.
- Tests: Plaid fixture with accounts sets balance and date; page without accounts leaves an existing balance alone; OFX with LEDGERBAL sets it (checking positive; card negated); CSV import leaves it unchanged; `FakePlaidClient` keeps working with old fixtures.
- [x] Commit: `Keep the bank-reported balance from Plaid and OFX feeds`

## Task 5: Bank register query and "Bank register" report

Files: `banking/register.py` (new), `banking/views/register.py` (new), `banking/templates/banking/report_bank_register.html` (new), `banking/templates/banking/_register_table.html` (new include), `ledger/urls.py`, `core/report_catalog.py`, `tests/test_bank_register.py` (new).

Interfaces:
```python
# banking/register.py
@dataclass
class RegisterFilters:
    start: date | None = None; end: date | None = None; type: str = ""   # a ledger.sources source_type, "manual", or ""
    q: str = ""; reconciled: str = ""                                     # "", "yes", "no"

@dataclass
class RegisterRow:
    line: JournalLine; date: date; reference: str; type_label: str; counterparty: str   # "Customer: Kabayan Foods"
    status: str          # "Reconciled" | "Matched" | "From bank" | "Manually added"
    deposit: Decimal | None; withdrawal: Decimal | None; balance: Decimal; url: str | None

def filtered_lines(account: BankAccount, f: RegisterFilters) -> QuerySet[JournalLine]
    # JournalLine.objects.in_ledger().filter(account=account.ledger_account) + filters, ordered newest first
    # (-entry__date, -entry_id, -id), select_related("entry"); annotated is_reconciled (Exists over
    # ReconciliationItem cleared=True, reconciliation status FINISHED).
def running_balances(account: BankAccount, line_ids: list[int]) -> dict[int, Decimal]
    # One SQL query: the window SUM(debit - credit) OVER (ORDER BY entry date, entry id, line id ROWS UNBOUNDED
    # PRECEDING) over ALL in-ledger lines of the account, computed in an inner query built from
    # JournalLine.objects.in_ledger().filter(account=...).annotate(Window(...)), compiled with
    # query.sql_with_params(), wrapped as SELECT id, running FROM (inner) WHERE id IN (...). money() each value;
    # negate for account.is_liability.
def build_rows(account: BankAccount, lines: list[JournalLine]) -> list[RegisterRow]
    # bulk: running_balances once; status via Match lookups (Match.journal_entry in entries, plus document matches
    # through source_type/source_id: sales.payment → Match.sales_payment__number, sales.deposit → deposit__number,
    # sales.refund → refund__number, purchasing.payment → vendor_payment__number), one query each;
    # counterparty via one bulk query per source type (Payment/Invoice/Refund/CreditNote → customer,
    # VendorPayment/Bill/VendorCredit → vendor, Deposit → "", Transfer → the other account name from memo-free
    # lookup of the entry's other lines in one query); "From bank" when the matching BankTransaction status is
    # BOOKED, "Matched" when MATCHED; deposit = amount that raises the shown balance, withdrawal = amount that
    # lowers it; reference = entry.reference or entry.source_id; url = describe(entry)["url"] or
    # reverse("books:journal-detail", args=[entry.pk]).
def ledger_balance(account: BankAccount) -> Decimal   # "Amount in books", from account_totals, same sign rule
TYPE_CHOICES: list[tuple[str, str]]   # [("", "All types"), ("manual", "Manual entry"), *(st, label) from ledger.sources._LABELS]
```
Search: `entry__memo__icontains | entry__reference__icontains | entry__source_id__icontains | description__icontains`, and if `q` parses as a Decimal, `| debit=amount | credit=amount`.

Report view `bank_register` (route `reports:bank-register` at `bank-register/`, catalog Accountant group: `("bank-register", "Bank register", "Every posted line on one bank or card account, with a running balance.")`): form with Account (select of bank accounts, required, empty state "Pick an account to see its register."), From, To, Type, Search, Reconciled. Default dates: none (whole history) unless given. Screen paginates 50 per page; `?format=csv` and PDF export every filtered row (no pagination). CSV `bank-register-{ledger number}.csv`: Date, Reference, Type, Counterparty, Status, Deposit, Withdrawal, Balance. Table include `_register_table.html` is shared with Task 6.

Tests (`tests/test_bank_register.py`):
- [x] every row's balance equals `signed balance of account_totals(start=None, end=row date)` restricted to lines up to and including that row (compute expected in the test by summing in Python over the account's ordered lines); newest row equals `ledger_balance`.
- [x] balances unchanged when filters (date range, type, search, reconciled) are applied.
- [x] a voided invoice payment: the original and its reversal both show and net to zero.
- [x] only in-ledger lines (a DRAFT entry on the account is absent).
- [x] each filter narrows correctly; amount search matches.
- [x] status values for a reconciled line, a matched payment, a line booked from the bank, and a manual entry.
- [x] liability (card) account sign.
- [x] query count of the report page flat (10 vs 60 lines).
- [x] CSV and PDF; catalog entry.
- [x] Screenshots: report with an account (light, dark), empty state, PDF first page.
- [x] Commit: `Bank register with a SQL running balance`

## Task 6: Bank account page

Files: `banking/views/account_page.py` (new), `banking/urls.py`, `banking/templates/banking/account_detail.html` (new), `banking/views/inbox.py` (extract `inbox_context`), `banking/templates/banking/_inbox_table.html` (new include), `banking/templates/banking/inbox.html`, `banking/templates/banking/accounts.html`, `ledger/books_views.py` + `ledger/templates/books/accounts.html`, `banking/views/accounts.py` (`sync_now` honors `next`), `core/static/core/app.css` (tab styles if none exist), `tests/test_bank_account_page.py` (new).

- Route `banking:account-detail` at `<int:pk>/`. `?tab=` dashboard (default) | uncategorized | all.
- Header and tiles exactly per spec 4.2 (strings: "Amount in books", "Amount in bank", "as of {date}", "statement, {date}", "Not reported", "Last feed date", "None yet", "Bank feeds last fetched on {Y-m-d H:i}", "Sync now", "File import", "Import statement", buttons "Add transaction" → `books:journal-new`, "Record deposit" → `sales:deposit-new`, "Quick categorize" → `banking:inbox` `?account={pk}`).
- Tabs as links with `aria-current`. Labels "Dashboard", "Uncategorized ({n})", "All transactions".
- Dashboard: cards "Last import" / "Last reconciliation" / "Transaction rules" (links "Import statement" → `banking:import-file`, "Reconcile" → `banking:reconcile-start`, "Rules" → `banking:rules`); counts "Total uncategorized", "Total recognized"; chart via `core.charts.line` + `core/_chart.html`, title "Closing balance, last 30 days"; "Recent transactions" = `build_rows` of the newest 10 through `_register_table.html`, "View more" → `?tab=all`. The 30 daily balances come from one opening aggregate and one `values("entry__date").annotate(Sum)` over in-ledger lines.
- Uncategorized: refactor `inbox_list` into `inbox_context(request, account_id: str, status: str) -> dict` used by both views; move the table + bulk form + foot into `_inbox_table.html` included by `inbox.html` and by this tab. Every form's `next` is the current full path, so actions return to the tab. Toggle link "Show excluded" / "Hide excluded" (switches `status=EXCLUDED` / `NEW`). Inbox page output unchanged (existing inbox tests pass untouched).
- All transactions: filters form (From, To, Type, Search, Reconciled) + `_register_table.html` + pagination 50 + "Download CSV" and "Download PDF" links to `reports:bank-register` with the same filters and `account={pk}`.
- `sync_now`: redirect to `request.POST.get("next")` when it starts with `/banking/`, else the accounts list.
- Links: Bank accounts list name → account page; chart of accounts row with a bank account → "Bank page" link beside "Ledger" (one query: `BankAccount.objects.values_list("ledger_account_id", "pk")`).
- Tests (`tests/test_bank_account_page.py`): tiles values (books equals register top row and `ledger_balance`; bank from `bank_balance`; fallback to reconciliation statement; "Not reported"); tab counts; uncategorized tab shows only this account's NEW rows and an exclude/restore round trip from the tab returns to it; excluded toggle; all-transactions tab running balance top row equals Amount in books; chart has 30 points ending today; links present on accounts list and chart of accounts; standard user can view, Sync now hidden for standard users; query count of each tab flat as rows grow.
- [x] Screenshots: each tab (light and dark), dashboard at 390px, account with no bank activity (empty states), Plaid-linked account header vs file-only header.
- [x] Commit: `Bank account page with dashboard, uncategorized, and register tabs`

## Task 7: README and demo data

Files: `README.md`, `core/management/commands/seed_demo.py` or the per-app `demo.py` modules (only if the screenshots needed data that the demo lacks; use services).
- [ ] README: add the new reports and the bank page to the Banking/Sales/Items task tables; update the Status line to mention this work as ready for review. Keep overview, roadmap, cutover criteria.
- [ ] Full `uv run pytest -q` green.
- [ ] Commit: `README for lot tracking, unpaid invoices, and the bank page`

## Task 8: Print margins (added by the owner mid-session)

Files: `sales/static/sales/invoice_pdf.css`, `sales/static/sales/statement_pdf.css`, `core/static/core/report_pdf.css`, `docs/design/invoice-mockup.html`, tests as needed.
- [x] Invoice PDF `@page` margin 0.25in top, left, right; 0.4in bottom (room for the page number). Content still fills the page width; nothing clips or wraps badly (long descriptions, many lines, multi-page with the repeated table header).
- [x] Statement PDF: same margins.
- [x] Report PDFs: 8mm top and sides, 12mm bottom.
- [x] Leave check PDFs and calibration alone (exact inches). Reconcile report and handling statement unchanged unless they look cramped or wasteful.
- [x] Update `docs/design/invoice-mockup.html` to the new margin.
- [x] Before and after screenshots (rendered PDFs, demo data): invoice, multi-page invoice, statement, one report.
- [x] Existing PDF tests pass; no invoice content changes.
- [x] Commit: `Tighter print margins on invoices, statements, and reports`

## Task 9: Mobile menu behind a hamburger (added by the owner mid-session)

Files: `core/templates/core/base.html`, `core/static/core/app.css`, tests.
- [ ] Under 900px the sidebar no longer stacks its full nav above the page. A compact top bar shows the brand and a "Menu" button (hamburger icon, `aria-expanded`, `aria-controls`); pressing it opens the nav, settings, theme toggle, and user block; pressing again or choosing a link closes it. Works without JavaScript (falls back to a `<details>`-style toggle or the nav stays reachable). Desktop unchanged.
- [ ] Test: base template renders the menu button with aria attributes; CSS hides the nav under 900px until opened.
- [ ] Screenshots at 390px: closed and open, light and dark; desktop unchanged.
- [ ] Commit: `Mobile menu behind a menu button`

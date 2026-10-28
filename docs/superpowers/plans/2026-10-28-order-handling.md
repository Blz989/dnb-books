# Order Profitability and Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Profit for every order, and a twice-monthly bill to the handling company for 55% of each order's profit not yet billed.

**Architecture:**
- **New `handling` app.** It depends on `sales`, `purchasing`, `items`, and `ledger`, and nothing depends on it except through registries (`ledger.locks`, `ledger.periods`) and one template tag that the sales detail pages load.
- **`handling/profit.py`** reads documents only and posts nothing.
- **`handling/billing.py`** turns a half-month period into one `HandlingRun`, plus one normal bill (or vendor credit) created through the existing `purchasing` posting functions. So the ledger is touched only through `ledger/posting.py` as usual.

**Tech Stack:** As in previous phases (Django 5.2, SQLite, WeasyPrint for the statement PDF). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-28-order-handling-design.md`. Visual reference: `docs/design/items-mockup.html`.

## Global Constraints

- **`CLAUDE.md` applies in full:** screenshots sent per task, a filled-in PR body, no skipped tests. Branch: `claude/order-handling`. PR title: `Order profitability and handling`.
- **Money:** `Decimal`, 2 dp, rounded half-up (`ROUND_HALF_UP`) when 55% is taken. Rates are stored as a fraction with 4 dp (`0.5500`) and shown as a percent (`55%`).
- **Handling owed per order** = `max(0.00, round(profit × rate, 2))`. It is never negative.
- **Each order's rate** is the rate in effect on the order date. The order date is the sales order's date, or a direct invoice's date.
- **Order key:** the sales order number (for example `SO-00123`), or the invoice number for an invoice with no sales order.
- **Periods:** the 1st to the 15th, and the 16th to the last day. Label format: `Oct 1–15, 2026` and `Oct 16–31, 2026`, with an en dash between the days, as in the spec. Long dates use the format `Oct 31, 2026`.
- **Exact strings:**

  | Where | Message |
  |---|---|
  | Settings incomplete | `Set up handling in Purchasing settings first.` |
  | Period out of order | `Bill {label} first.` |
  | Period already billed | `{label} is already billed.` |
  | Period not ended | `{label} can't be billed until {long date of period end}.` |
  | Void, not the latest | `Only the most recent handling bill can be voided.` |
  | Void, no reason | `Enter a reason.` |
  | Lock | `{bill or credit number} is the handling bill for {label}. Void it from Handling.` |
  | Close check | `{Month YYYY}: the handling bill for {label} hasn't been created.` |
  | Rate, bad value | `Enter a rate between 0 and 100%.` |
  | Rate, bad date | `The new rate must start after {long date}.` |
  | Flags | `Drop-ship bill not entered`, `Below zero, no handling`, `Before handling start` |
  | Home card | `Handling bill due for {label}` |
- **Bills and credits** are created only with:
  - `purchasing.posting.bills.create_bill`. Pass `allow_duplicate=True`, because a period billed again after a void reuses its vendor invoice number `HND-YYYYMMDD`.
  - `purchasing.posting.credits.create_allowance_credit`.
- **Permissions:**
  - Any signed-in user can view everything and create a run.
  - Voiding a run, the handling settings, and adding a rate are admin only (`core.roles.require_admin`).

## Review Focus

1. **A run is voided, then the period is billed again.** `billed_before` ignores void runs. The new bill reuses `HND-YYYYMMDD` without a duplicate refusal, and the void run's statement stays viewable. Pinned in Task 4.
2. **A drop-ship bill dated inside an already-billed period, but entered after that run.** The next run computes owed as of its own end date, so the cost is included and the order's row shows a negative `this_bill`. Pinned in Task 3.
3. **A credit moved from order A's invoice to order B's invoice between runs.** The next run shows A negative and B positive, and the total nets them. Pinned in Task 3.
4. **An order that lost money in the first period and made money in the second.** The first run bills 0.00, so the order is left off. The second bills 55% of the cumulative profit, and the earlier loss is not carried as a credit. Pinned in Task 3.
5. **Create bill clicked twice, or from two tabs.** Only one run exists for the period. The second request gets `{label} is already billed.`, or `That was already done.` for the same form token. Pinned in Task 3 (service) and Task 6 (view).

---

## File Structure

```
handling/
  apps.py            registers the lock check and close check in ready()
  models.py          HandlingSettings, HandlingRate, HandlingRun, HandlingRunLine
  rates.py           rate_for, add_rate
  profit.py          OrderProfit, order_profit, orders, billed_to_date
  periods.py         Period, next_to_bill, due_periods
  billing.py         HandlingError, preview, create_run, void_run, statement_pdf
  checks.py          run_lock, close_check
  demo.py            seed_handling (called from core seed_demo)
  forms.py  urls.py  views.py
  templatetags/handling_tags.py   {% profit_panel key %}
  templates/handling/*.html  static/handling/statement_pdf.css
dnb/settings.py, dnb/urls.py       add the app; include handling.urls at "handling/"
ledger/urls.py                     reports:order-profitability
core/views.py, core/templates/core/home.html, core/templates/core/base.html   card, links, nav
sales/templates/sales/order_detail.html, invoice_detail.html                  profit panel
purchasing/templates/purchasing/settings.html                                 Handling section (link)
core/management/commands/seed_demo.py                                         seed_handling
tests/ test_handling_models.py, test_handling_profit.py, test_handling_runs.py,
       test_handling_locks.py, test_handling_views.py, test_handling_acceptance.py
```

Test setup throughout: reuse the fixtures in `tests/conftest.py`: `admin_user`, `standard_user`, `customer`, `vendor`, `expense`, `inv_settings`, `sales_settings`, `purchasing_settings`, and `make_item`. Reuse the drop-ship helpers in `tests/test_dropship.py`: `create_order` with `Fulfillment.DROP_SHIP`, `create_drop_ship_pos`, `invoice_from_order`, and `bill_from_po`. Stock comes in through a vendor bill, like `tests/test_bills.py::receive`. Put shared helpers in a `handling_setup` fixture in `tests/conftest.py`. It creates the vendor `Harbor Handling LLC`, the account `6400 Handling` (expense), a start date of 2026-10-01, and a rate of 0.5500 from 2026-10-01.

---

### Task 1: App, models, settings, and rates

**Files:**
- Create: `handling/__init__.py`, `apps.py`, `models.py`, `rates.py`, `migrations/0001_initial.py`
- Modify: `dnb/settings.py` (`INSTALLED_APPS`), `core/management/commands/setup_roles.py` (Standard gets view on the handling models, plus add on `HandlingRun` and `HandlingRunLine`)
- Test: `tests/test_handling_models.py`

**Interfaces:**
- Produces:
  - `HandlingSettings.load() -> HandlingSettings` (single row, like `PurchasingSettings`), with fields `vendor`, `expense_account`, and `start_date` (all nullable), and the property `is_configured -> bool`. It is configured when all three are set and at least one rate exists.
  - `HandlingRate(effective_from: date unique, rate: Decimal(6,4), created_by, created_at)`.
  - `HandlingRun(period_start, period_end, total, bill?, vendor_credit?, status: RunStatus{POSTED, VOID}, statement_pdf: FileField(upload_to="handling/"), created_by, created_at, voided_by?, voided_at?, void_reason)`.
    - Constraint: `UniqueConstraint(fields=["period_start"], condition=Q(status="POSTED"), name="one_posted_run_per_period")`. This refines the spec's "unique together", because the spec also lets a voided period be billed again.
    - Property `label -> str`.
  - `HandlingRunLine(run, order_key, sales_order?, invoice?, customer, sales, cost, profit, rate, owed_to_date, billed_before, this_bill)`.
  - In `handling/rates.py`:
    - `rate_for(day: date) -> Decimal | None`: the rate with the latest `effective_from <= day`.
    - `add_rate(*, effective_from: date, percent: Decimal, user) -> HandlingRate`: admin only. Refuses a percent outside 0 to 100 with `Enter a rate between 0 and 100%.` Refuses a date on or before the latest existing date with `The new rate must start after {long date}.` Stores `percent / 100` with 4 dp.

- [x] **Step 1: Write the failing tests** in `tests/test_handling_models.py`:
  - `test_settings_not_configured_until_vendor_account_start_and_rate`
  - `test_rate_for_uses_latest_effective_on_or_before`: rates of 55% from Oct 1 and 50% from Nov 1. `rate_for(Oct 31) == Decimal("0.5500")`, `rate_for(Nov 1) == Decimal("0.5000")`, and `rate_for(Sep 30) is None`.
  - `test_add_rate_stores_fraction` (55 gives `0.5500`) and `test_add_rate_refuses_bad_percent` (−1 and 101).
  - `test_add_rate_must_start_after_latest`
  - `test_add_rate_is_admin_only`: `PermissionDenied` for `standard_user`.
  - `test_only_one_posted_run_per_period`: a second POSTED run with the same `period_start` raises `IntegrityError`, but a VOID one plus a POSTED one is allowed.
  - `test_run_label`: Oct 16 to 31, 2026 gives `"Oct 16–31, 2026"`.
- [x] **Step 2:** Run `uv run pytest tests/test_handling_models.py -v`. Expect it to fail on the import of `handling`.
- [x] **Step 3:** Implement the models, `rates.py`, and the migration (`uv run python manage.py makemigrations handling`). Register `HandlingSettings`, `HandlingRate`, and `HandlingRun` with auditlog, as the other apps do.
- [x] **Step 4:** Run `uv run pytest tests/test_handling_models.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: handling app, settings, and rates`.

### Task 2: Profit per order

Use the `finance:reconciliation` skill to review that the profit figures tie to the documents. Record the findings in the commit body.

**Files:**
- Create: `handling/profit.py`
- Test: `tests/test_handling_profit.py`

**Interfaces:**
- Consumes:
  - `rate_for` from Task 1.
  - `items.stock.effective_total(movement) -> Decimal`. A sale's movement is negative, so its cost is `-effective_total(m)`.
  - `purchasing.models.DropShipAllocation`.
  - `sales.models.CreditApplication`.
- Produces:
  - `LineCost(invoice_line, kind: Literal["stock", "drop_ship"], cost: Decimal, pending: bool)`
  - `InvoiceProfit(invoice, sales: Decimal, credits: Decimal, lines: list[LineCost])`
  - `OrderProfit(key: str, sales_order: SalesOrder | None, invoice: Invoice | None, customer, date, sales, credits, cost, profit, rate: Decimal | None, owed: Decimal, billed: Decimal, flags: list[str], invoices: list[InvoiceProfit])`, with properties `net_sales` (sales − credits), `margin` (`profit / net_sales × 100`, 1 dp, or `None` when `net_sales` is 0), and `balance` (owed − billed).
  - `order_profit(key: str, as_of: date) -> OrderProfit`
  - `orders(as_of: date, *, date_from: date | None = None, date_to: date | None = None, customer=None) -> list[OrderProfit]`: every order with at least one invoice dated on or before `as_of`, filtered by order date, sorted by order date then key.
  - `billed_to_date(key: str) -> Decimal`: the sum of `this_bill` on lines of POSTED runs.

Rules from spec 5.1, with these implementation choices:
- **Sales.** Use `InvoiceLine.amount` on the invoice's current lines. An invoice with `status == VOID` and `voided_on <= as_of` contributes 0.00, in sales, credits, and cost.
- **Credits.** Use `CreditApplication` rows on the order's invoices with `applied_on <= as_of`, excluding rows with `removed=True and removed_on <= as_of`.
- **Stock cost.** Use `-effective_total(line.stock_movement)` for lines with a `stock_movement`.
- **Drop-ship cost.** For each allocation on the line whose bill is OPEN and has `bill.date <= as_of`, the cost is `round(bill_item_line.amount × allocation.quantity / bill_item_line.quantity, 2)`. The line is `pending` when its allocated quantity is less than its quantity. Any pending line adds the flag `Drop-ship bill not entered`.
- **Flags.**
  - Add `Below zero, no handling` when profit < 0.
  - When the order date is before `start_date`, add `Before handling start`, and owed is 0.00.
  - When there is no rate for the order date, owed is 0.00.

- [x] **Step 1: Write the failing tests** in `tests/test_handling_profit.py`:
  - `test_stock_order`: receive 10 at 4.00, sell 3 at 9.50. Sales 28.50, cost 12.00, profit 16.50, owed 9.08 (55% of 16.50 = 9.075, half-up).
  - `test_drop_ship_order_with_bill`: drop-ship 3 at 9.50, billed at 4.00. Profit 16.50.
  - `test_drop_ship_line_without_bill_is_pending`: cost 0.00, profit 28.50, flag `Drop-ship bill not entered`.
  - `test_mixed_order_over_two_invoices`: one stock line and one drop-ship line, invoiced 1 then 2. Sales and cost both sum across the invoices.
  - `test_direct_invoice_is_its_own_order`: the key is the invoice number, and the rate comes from the invoice date.
  - `test_edited_invoice_counts_current_lines`
  - `test_voided_invoice_counts_zero_from_void_date`: as of the day before the void, it counts; as of the void date, it counts 0.00.
  - `test_credit_applied_reduces_sales`: apply 5.00 to the invoice. Profit falls by 5.00.
  - `test_credit_removed_then_applied_to_other_order`: as of the day between, A is reduced. After the move, B is reduced and A is not.
  - `test_fifo_replay_changes_cost`: a backdated cheaper receipt lowers the stock cost.
  - `test_as_of_excludes_later_documents`: an invoice dated after `as_of` isn't counted, and neither is a drop-ship bill dated after `as_of`.
  - `test_loss_owes_zero`: owed 0.00, flag `Below zero, no handling`.
  - `test_rate_by_order_date`: an order dated Oct 31 uses 55% even when invoiced on Nov 3 after a 50% rate from Nov 1.
  - `test_before_start_owes_zero`
  - `test_orders_filters_by_date_and_customer`
- [x] **Step 2:** Run `uv run pytest tests/test_handling_profit.py -v`. Expect FAIL.
- [x] **Step 3:** Implement `handling/profit.py`. Batch the queries per `orders()` call using `select_related` and `prefetch_related` on invoice lines, allocations, and applications, so the report stays at a handful of queries per page, not per order.
- [x] **Step 4:** Run `uv run pytest tests/test_handling_profit.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: profit per order`.

### Task 3: Periods and creating a run

Use the `finance:journal-entry` skill to review the bill and credit postings. Record the findings in the commit body.

**Files:**
- Create: `handling/periods.py`, `handling/billing.py`, `handling/templates/handling/statement_pdf.html`, `handling/static/handling/statement_pdf.css`
- Test: `tests/test_handling_runs.py`

**Interfaces:**
- Consumes: `orders`, `order_profit`, and `billed_to_date` from Task 2. `create_bill(..., expense_lines=[BillExpenseSpec(account, amount, description)], allow_duplicate=True)`. `create_allowance_credit(vendor=, date=, lines=[AllowanceLineSpec(account, amount, description)], reason=, user=)`.
- Produces:
  - `Period(start: date, end: date)`, frozen, with `label`, `Period.containing(day) -> Period`, and `next() -> Period`.
  - `next_to_bill() -> Period | None`: the period after the latest POSTED run's period. With no runs, it is `Period.containing(start_date)`. It is `None` when the settings aren't configured.
  - `due_periods(today: date) -> list[Period]`: from `next_to_bill()` onward, every period whose `end <= today`.
  - `class HandlingError(Exception)`
  - `RunRow(order: OrderProfit, owed_to_date, billed_before, this_bill)` and `Preview(period, rows: list[RunRow], total: Decimal)`
  - `preview(period: Period) -> Preview`: rows for orders dated from `start_date` to `period.end`, computed `as_of=period.end`, leaving out rows where `this_bill == 0`.
  - `create_run(period: Period, *, user, today: date | None = None) -> HandlingRun`
  - `statement_pdf(run) -> bytes`

`create_run`, inside one `transaction.atomic()`:
1. Refuse with `Set up handling in Purchasing settings first.` if the settings are incomplete.
2. Recompute `next_to_bill()`. If `period` is earlier than it, refuse with `{label} is already billed.` If later, refuse with `Bill {next label} first.`
3. If `period.end > today`, refuse with `{label} can't be billed until {end}.`
4. Build the preview.
5. Create the document, dated `period.end`:
   - Total > 0: a bill with vendor invoice number `HND-{end:%Y%m%d}`, one expense line `Handling {label}`, and the notes `Handling statement {label}`.
   - Total < 0: an allowance credit for `-total` with the reason `Handling corrections {label}`.
   - Total = 0: no document.
6. Save the run and its lines, render the statement PDF to `statement_pdf`, and return the run.

- [x] **Step 1: Write the failing tests** in `tests/test_handling_runs.py`:
  - `test_periods_and_labels`: `Period.containing(date(2026,2,20))` is Feb 16 to 28, labeled `Feb 16–28, 2026`. `next()` after Oct 16 to 31 is Nov 1 to 15.
  - `test_first_period_contains_start_date`
  - `test_create_run_posts_one_bill_for_the_total`: the bill's vendor is the handling vendor, dated period end, number `HND-20261015`. The expense line is on `6400` for the total, and the run lines match the preview.
  - `test_ap_check_stays_zero_after_run`: `purchasing.checks.ap_check(period.end) == []`.
  - `test_period_must_have_ended` (uses `today`), `test_periods_in_order`, and `test_period_billed_once`, each with its exact message.
  - `test_zero_total_records_run_without_document`
  - `test_negative_total_creates_vendor_credit`: bill period 1, then apply a large credit in period 2 with no new sales.
  - `test_late_drop_ship_bill_corrected_next_run` (Review Focus 2): drop-ship invoiced Oct 10 with no bill. Run Oct 1–15 bills 15.68 (55% of 28.50). Then a bill dated Oct 12 is entered on Oct 20. Run Oct 16–31 shows that order with `this_bill = -6.60`.
  - `test_credit_moved_between_orders` (Review Focus 3)
  - `test_loss_then_profit` (Review Focus 4)
  - `test_second_create_for_same_period_refused` (Review Focus 5): `{label} is already billed.`, and exactly one POSTED run.
  - `test_settings_incomplete_refused`
  - `test_statement_pdf_lists_rows`: `pdf.startswith(b"%PDF")`, and the rendered HTML contains each order key and the total.
- [x] **Step 2:** Run `uv run pytest tests/test_handling_runs.py -v`. Expect FAIL.
- [x] **Step 3:** Implement `periods.py`, `billing.py`, and the statement template. The statement follows `sales/pdf.py`: bundled fonts and the Ledger paper style. It shows the title `Handling statement, {label}`, DNB's name and address from `SalesSettings`, the handling vendor's name, and the columns order, customer, sales, cost, profit, owed to date, billed before, and this bill, with a total row.
- [x] **Step 4:** Run `uv run pytest tests/test_handling_runs.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: handling periods and runs`.

### Task 4: Voiding, the lock on handling bills, and the month close check

**Files:**
- Create: `handling/checks.py`
- Modify: `handling/billing.py` (`void_run`), `handling/apps.py` (`ready()` registers both checks)
- Test: `tests/test_handling_locks.py`

**Interfaces:**
- Consumes: `ledger.locks.register_lock_check`, `ledger.periods.register_close_check`, `purchasing.posting.bills.void_bill`, and `purchasing.posting.credits.void_vendor_credit`.
- Produces:
  - `void_run(run, *, user, reason: str) -> HandlingRun`: admin only. Refuses a blank reason with `Enter a reason.` and any run but the latest POSTED one with `Only the most recent handling bill can be voided.` Then, in one transaction, it marks the run VOID first (so `run_lock` lets the void through) and then voids its bill or credit, dated on the document's own date. Any `PurchasingError` from voiding is re-raised as `HandlingError` with the same message, so a paid bill shows the Phase 4 message `{number} has payments or credits applied. Remove them first.` (Spec 5.4 refers to this as "the existing Phase 4 rule". The existing wording is used.)
  - `run_lock(entry) -> str | None`: for an entry with `source_type` `purchasing.bill` or `purchasing.vendor_credit` whose `source_id` is the bill or credit number of a POSTED run, it returns `{number} is the handling bill for {label}. Void it from Handling.` Otherwise it returns `None`.
  - `close_check(year: int, month: int) -> list[str]`: for months on or after the start date's month, one message per period in that month without a POSTED run.

- [x] **Step 1: Write the failing tests** in `tests/test_handling_locks.py`:
  - `test_void_latest_run_voids_its_bill_and_frees_the_period`
  - `test_void_only_latest`
  - `test_void_needs_reason_and_admin`
  - `test_void_refused_when_bill_paid`: pay the bill with `pay_bills`. The message is `{bill} has payments or credits applied. Remove them first.`, and the run is still POSTED.
  - `test_rebill_after_void` (Review Focus 1): void, then create again. The new bill has the same `HND-` vendor number, `billed_before` ignores the void run, and the totals equal the first run's.
  - `test_handling_bill_cannot_be_voided_from_bills`: `void_bill` directly raises with the lock message.
  - `test_handling_bill_cannot_be_edited`: `edit_bill` raises with the lock message.
  - `test_handling_credit_locked_too`
  - `test_ordinary_bills_unaffected`
  - `test_close_blocked_until_both_periods_billed`: `close_period(2026, 10, user)` raises `PeriodCloseBlockedError` naming `Oct 16–31, 2026`, and closes after both runs exist.
  - `test_close_override_with_reason`
  - `test_months_before_start_not_checked`
- [x] **Step 2:** Run `uv run pytest tests/test_handling_locks.py -v`. Expect FAIL.
- [x] **Step 3:** Implement `void_run`, `checks.py`, and the registrations.
- [x] **Step 4:** Run `uv run pytest tests/test_handling_locks.py tests/test_bills.py tests/test_vendor_credits.py -v`. Expect PASS. Ordinary bill behavior must be unchanged.
- [x] **Step 5:** Commit with `feat: void handling runs, lock handling bills, month close check`.

### Task 5: Order profitability report, profit panel, and settings screens

**Files:**
- Create: `handling/forms.py`, `handling/views.py` (report and settings parts), `handling/urls.py`, `handling/templatetags/handling_tags.py`, `handling/templates/handling/{order_profitability,profit_panel,settings}.html`, `handling/demo.py`
- Modify:
  - `dnb/urls.py`: `path("handling/", include("handling.urls"))`
  - `ledger/urls.py`: `path("order-profitability/", handling_views.order_profitability, name="order-profitability")`
  - `core/templates/core/home.html`: an "Order profitability" link under Reports
  - `sales/templates/sales/order_detail.html`: `{% load handling_tags %}{% profit_panel order.number %}`
  - `sales/templates/sales/invoice_detail.html`: the panel, only when `invoice.sales_order` is empty
  - `purchasing/templates/purchasing/settings.html`: a Handling section with the vendor, account, start date, and current rate, plus a link to `/handling/settings/`
  - `core/management/commands/seed_demo.py`: call `seed_handling(user)`
- Test: `tests/test_handling_views.py` (report and settings part)

**Interfaces:**
- Consumes: `orders`, `order_profit`, and `add_rate`.
- Produces:
  - URL `reports:order-profitability`. GET params: `from`, `to` (ISO dates, default the first of this month through today), `customer` (pk), and `unbilled` (`1`).
  - URL `handling:settings` (admin only).
  - Template tag `profit_panel(key)`.
  - `seed_handling(user) -> None`: creates the vendor `Harbor Handling LLC`, the account `6400 Handling`, a start date at the first day of the earliest demo order's month, and a 55% rate.
- **Report columns:** Order, Customer, Date, Sales, Cost, Profit, Margin, Handling owed, Billed, Balance. There is a totals row. Each flag shows as a small tag in the Order cell, styled like the `1 below zero` tag in the sidebar. `unbilled=1` keeps only rows whose balance is not 0.00. "Sales" is net of credits, with credits shown as a second line in the profit panel only.
- **Profit panel:** for each invoice, its sales, credits, and lines (with stock or drop-ship cost, and `pending` shown as `Drop-ship bill not entered`). Then profit, rate as a percent, handling owed, and each POSTED run that billed it (label, linked, and amount).
- **Settings page:**
  - Vendor (select), expense account (expense accounts only), and start date. Changing the start date is refused once any POSTED run exists, with `The start date can't change after the first handling bill.`
  - The rate history table, and an Add rate form (effective from, percent).
  - On first save with no rates, a 55% rate from the start date is created.

- [x] **Step 1: Write the failing tests** in `tests/test_handling_views.py`:
  - `test_report_lists_orders_with_totals`
  - `test_report_filters_customer_dates_unbilled`
  - `test_report_shows_flags`
  - `test_profit_panel_on_order_and_direct_invoice` (not on an invoice that has an order)
  - `test_settings_admin_only`
  - `test_settings_first_save_creates_55_percent_rate`
  - `test_add_rate_from_settings`
  - `test_start_date_locked_after_first_run`
  - `test_standard_user_can_view_report`
- [x] **Step 2:** Run `uv run pytest tests/test_handling_views.py -v`. Expect FAIL.
- [x] **Step 3:** Implement. Match `docs/design/items-mockup.html` (tokens, table style, tags, and the Auto/Light/Dark toggle comes from `base.html`).
- [x] **Step 4:** Run `uv run pytest tests/test_handling_views.py -v`. Expect PASS.
- [x] **Step 5: Screenshots.** Use a demo database with `seed_demo`, then `scripts/screenshots.mjs`, in light and dark. Capture:
  - the report, unfiltered and with `unbilled=1`, including a flagged row
  - the profit panel on a sales order with a drop-ship line
  - the panel on a direct invoice
  - the settings page

  Send them with `SendUserFile`, with a one-line caption each.
- [x] **Step 6:** Commit with `feat: order profitability report, profit panel, handling settings`.

### Task 6: Handling run screens and home card

**Files:**
- Modify: `handling/views.py`, `handling/urls.py`, `core/views.py`, `core/templates/core/home.html`, `core/templates/core/base.html` (a sidebar link "Handling" under Purchasing, `nav_active == "handling"`)
- Create: `handling/templates/handling/{run_list,preview,run_detail}.html`
- Test: `tests/test_handling_views.py` (runs part)

**Interfaces:**
- Consumes: `next_to_bill`, `due_periods`, `preview`, `create_run`, `void_run`, `statement_pdf`, and the form-token helpers `items.views._claim_token` and `sales.views.common.new_token`.
- Produces these URLs:
  - `handling:runs` (`/handling/`)
  - `handling:preview` (`/handling/preview/<period_end:YYYY-MM-DD>/`, GET shows the preview, POST creates)
  - `handling:run` (`/handling/runs/<pk>/`)
  - `handling:run-void` (POST, admin)
  - `handling:statement` (`/handling/runs/<pk>/statement.pdf`, served inline)
- **Run list:** Period, Total, Bill or credit (linked), Status. The button `Next: {label}` links to the preview, and is disabled with the text `{label} can't be billed until {date}.` until the period ends.
- **Preview:** the columns order, customer, sales, cost, profit, owed to date, billed before, and this bill, then the total. The button is `Create bill` (or `Create credit` when the total is negative, or `Record period` when it is 0.00). A one-time token protects it. When the settings are incomplete, the page shows `Set up handling in Purchasing settings first.` with a link (admin) instead of the table.
- **Run detail:** the saved rows, a link to the bill or credit, a statement PDF link, and, for the latest POSTED run when the user is an admin, a Void form with a reason.
- **Home card:** when `due_periods(today)` is not empty, show `Handling bill due for {first label}` linking to its preview. Settings incomplete means no card.

- [x] **Step 1: Write the failing tests** (append to `tests/test_handling_views.py`):
  - `test_run_list_and_next_button`
  - `test_preview_then_create` (redirects to the run detail, and the bill exists)
  - `test_create_double_post_same_token_makes_one_run` (Review Focus 5): `That was already done.`
  - `test_create_credit_label_when_negative`
  - `test_void_from_detail_admin_only`
  - `test_statement_served_inline`
  - `test_home_card_due_and_hidden`
  - `test_standard_user_can_create_run`
- [x] **Step 2:** Run `uv run pytest tests/test_handling_views.py -v`. Expect FAIL.
- [x] **Step 3:** Implement.
- [x] **Step 4:** Run `uv run pytest tests/test_handling_views.py -v`. Expect PASS.
- [x] **Step 5: Screenshots** in light and dark:
  - the run list, with the Next button enabled and with it disabled
  - the preview
  - the preview with incomplete settings
  - the run detail with the Void form
  - the home card
  - the statement PDF, rasterized

  Send each with a caption.
- [x] **Step 6:** Commit with `feat: handling run screens and home card`.

### Task 7: Acceptance, README, and PR

**Files:**
- Create: `tests/test_handling_acceptance.py`
- Modify: `README.md` (feature list, Status line), `CLAUDE.md` (Done list)

- [x] **Step 1: Write `test_handling_month`**, following spec success criterion 1. Run through October 2026:
  - Ship and invoice a stock order, a drop-ship order with its line invoiced before its bill, and a mixed order in two invoices.
  - Run Oct 1–15, and check each row by hand: write each expected figure as a literal.
  - Enter the late drop-ship bill, apply a customer credit, and edit an invoice.
  - Run Oct 16–31, and check the correction rows.
  - Pay both bills with `pay_bills`.
  - `close_period(2026, 10, admin_user)` succeeds.
  - Check, after each step, that `ap_check`, `ar_check`, `undeposited_check`, and `items.stock.check_invariants()` are empty.
  - Finally, check that the sum of POSTED run bill totals, less credit totals, equals the ledger balance of `6400` (`JournalLine.objects.in_ledger()`, debit − credit), which is success criterion 2.
- [x] **Step 2:** Run `uv run pytest tests/test_handling_acceptance.py -v`. Expect PASS. If it fails, fix the code in the task that owns it.
- [x] **Step 3:** Run `uv run pytest -q`. Every test must pass.
- [x] **Step 4:** Update `README.md`: add Order profitability and Handling to the feature list, and update the Status line. In `CLAUDE.md`, add "Order profitability and handling" to Done.
- [x] **Step 5:** Commit with `test: order handling acceptance; docs`.
- [ ] **Step 6:** Open the PR `Order profitability and handling` into `main`. Its body follows `CLAUDE.md`:
  - the tasks with their status
  - the final pytest output
  - any deviations
  - the screenshots sent
  - the manual steps for the owner: create the handling vendor and the `Handling` expense account on the server, set the start date and confirm the 55% rate in Purchasing settings, and then create the first handling bill

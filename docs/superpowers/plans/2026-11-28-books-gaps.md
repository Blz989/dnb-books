# Attachments, sales and purchase reports, recurring transactions, cash flow statement, bad debt write-off: plan

Spec: `docs/superpowers/specs/2026-11-28-books-gaps-design.md`.
PR title: "Attachments, sales and purchase reports, recurring bills and journals, cash flow statement, bad debt write-off"

## Global Constraints
- No new dependencies. File type checks read the first bytes (no python-magic).
- Money is `Decimal`. All ledger writes go through `ledger/posting.py`; new code calls existing document functions (`create_bill`, `create_credit_note`, `apply_credit`) or `post_entry` only.
- Reports in this plan read posted ledger lines (`JournalLine.objects.in_ledger()`); the two by-item reports read document lines only with the ledger tie-out the spec (2.2) requires, as A/R aging does today.
- Existing tests pass untouched, except the report group count in `tests/test_reports_forecast.py` (5 to 6), which the new "Sales and purchases" group changes, and the scheduler job set in `tests/test_plaid_sync.py`, which gains `run_recurring`.
- Error messages match the spec exactly.
- Screenshots from a throwaway demo database; never the real `data/` database.

## Review Focus
- Recurring runs: never two documents for the same profile and date (unique constraint plus a check inside the transaction); a failure stops only that profile; catch-up order.
- Month-end date math (Jan 31 start).
- By-item reports: the void rule makes Difference 0.00 across ranges that split an invoice date and its void date.
- Cash-basis P&L: a write-off moves nothing; an ordinary credit note is unchanged.
- Cash flow statement: Difference 0.00 on the demo database and on a ledger with every subtype.
- Attachment downloads need sign-in; only allowed types are stored.

## Plan-level decisions (mechanical, within the spec)
- `Attachment` lives in a new `core/models.py` with a generic relation (`content_type`, `object_id` as a positive integer pk). The registry `core/attachments.py` maps a short kind ("invoice", "bill", ...) to the model and how the detail page finds it.
- Recurring bill models live in `purchasing`, recurring journal models in `ledger`; the date math and the runner are shared in `ledger/recurring.py`.
- `run_recurring` posts as the profile's `created_by`.
- Write-off credit notes use the credit note number series; `CreditNote.write_off_of` (FK Invoice, null) marks them and names the invoice.

## Task 1: Attachments
Interfaces:
```python
# core/models.py
class Attachment(models.Model):
    content_type: FK ContentType
    object_id: PositiveIntegerField
    file: FileField(upload_to=attachment_path)   # attachments/<yyyy>/<mm>/<uuid><ext>
    name: CharField(255)                        # original file name
    size: PositiveIntegerField
    mime: CharField(50)
    uploaded_by: FK User
    uploaded_at: DateTimeField(auto_now_add)
# core/attachments.py
KINDS: dict[str, Kind]          # kind -> model, label
def for_object(obj) -> QuerySet[Attachment]
def add(obj, upload, *, user) -> Attachment        # raises AttachmentError(spec 1.4 message)
def remove(attachment, *, user) -> None             # admin only
def sniff(head: bytes) -> str | None                # "application/pdf" | "image/png" | "image/jpeg"
def context(obj, request) -> dict                   # for the _attachments.html include
```
- [x] Model, migration, auditlog registration.
- [x] Service with validation (empty, type by bytes, 10 MB) and admin-only remove.
- [x] Views `/attachments/add/<kind>/<pk>/` (POST), `/attachments/<pk>/` (GET, login, inline, nosniff), `/attachments/<pk>/remove/` (GET confirm, POST remove, admin). Redirect back to the record's page.
- [x] `core/_attachments.html` card on the 11 detail pages named in spec 1.1.
- [x] Tests `tests/test_attachments.py`.
- [x] Screenshots: bill with two attachments, upload error, remove confirm, journal entry and vendor pages with the card.
- [x] Commit: `Attachments on documents, customers, and vendors`

## Task 2: Sales by customer and purchases by vendor
Interfaces:
```python
# ledger/sales_reports.py
@dataclass class CustomerSalesRow: customer, invoices: int, sales, credits, net, written_off
@dataclass class SalesByCustomer: start, end, rows, total: CustomerSalesRow-like totals, other_income, pnl_income, difference
def sales_by_customer(start, end) -> SalesByCustomer
@dataclass class VendorPurchasesRow: vendor, bills: int, purchases, credits, net
@dataclass class PurchasesByVendor: start, end, rows, totals..., ap_total, difference
def purchases_by_vendor(start, end) -> PurchasesByVendor
```
- [x] Report functions from ledger lines (spec 2.1, 2.2). `written_off` stays 0 until Task 7 adds `write_off_of`; Task 7 wires it.
- [x] Views, templates, CSV, PDF, catalog group "Sales and purchases".
- [x] Tests `tests/test_sales_purchase_reports.py`: invoice, edit across dates, void in a later month, credit note, journal income, Difference 0.00; bills and vendor credits.
- [x] Screenshots: both reports, empty range.
- [x] Commit: `Sales by customer and purchases by vendor reports`

## Task 3: Sales by item and purchases by item
Interfaces:
```python
# ledger/sales_reports.py
@dataclass class ItemSalesRow: item, quantity: int, amount, average
def sales_by_item(start, end, customer=None) -> SalesByItem    # rows, total, ledger_total, difference
@dataclass class ItemPurchaseRow: item | None, account | None, quantity: int | None, amount, average
def purchases_by_item(start, end, vendor=None) -> PurchasesByItem
```
- [x] Document lines with the void rule (spec 2.1); ledger tie-out (spec 2.2). With a customer or vendor filter, the tie-out uses that customer's or vendor's entries.
- [x] Views, templates, CSV, PDF, catalog.
- [x] Tests in `tests/test_sales_purchase_reports.py`: void split across ranges, edit changing quantity, adjustment line, filters, Difference 0.00.
- [x] Screenshots: both reports, with a filter.
- [x] Commit: `Sales by item and purchases by item reports`

## Task 4: Recurring engine
Interfaces:
```python
# ledger/recurring.py
class Frequency(TextChoices): WEEKLY, BIWEEKLY, MONTHLY, QUARTERLY, SEMIANNUAL, YEARLY
def occurrence(start: date, frequency: str, n: int) -> date     # n = 0 is start
def due_dates(profile, today: date) -> list[date]                # not yet created, <= today, <= end
def next_date(profile, today: date) -> date | None
def run_all(today: date) -> RunSummary                           # created, failed
# purchasing/models.py
class RecurringBill: name, vendor, terms, notes, frequency, start_date, end_date, is_active, resume_from,
                     last_error, last_error_date, created_by, created_at
class RecurringBillLine: profile, account, description, amount, position
class RecurringBillRun: profile, scheduled_date, bill (FK), created_at; unique(profile, scheduled_date)
# ledger/models.py
class RecurringJournal: name, memo, frequency, start_date, end_date, is_active, resume_from, last_error,
                        last_error_date, created_by, created_at
class RecurringJournalLine: profile, account, description, debit, credit, position
class RecurringJournalRun: profile, scheduled_date, entry (FK), created_at; unique(profile, scheduled_date)
# ledger/management/commands/run_recurring.py
# backup/management/commands/run_scheduler.py: daily job at DNB_RECURRING_TIME, and once at start
```
- [x] Models, migrations, auditlog registration.
- [x] Date math (spec 3.1) and runner (spec 3.2): oldest first, stop the profile on failure, record error, clear error on success.
- [x] `resume_from`: set on Resume to today; due dates before it are skipped.
- [x] Scheduler job and `DNB_RECURRING_TIME` setting (default "05:00", in `.env.example`).
- [x] Tests `tests/test_recurring.py`: month-end math, every frequency, catch-up, end date, pause and resume, closed month error then recovery, no duplicates on a second run, inactive account error.
- [x] Commit: `Recurring bills and journal entries: schedules and nightly runs`

## Task 5: Recurring screens, Home, and cash forecast
- [x] Purchasing, Recurring bills: list, new, page, edit, pause or resume, delete (spec 3.3, 3.6). Nav link under Purchases.
- [x] Accountant, Recurring journal entries: the same. Link on the Accountant area.
- [x] Home: "Created by recurring schedules" card (last 7 days) and error banners.
- [x] Cash forecast: upcoming recurring bill occurrences as outflows; note text (spec 3.4).
- [x] Tests `tests/test_recurring_views.py`.
- [x] Screenshots: both lists, both forms with an error, a profile page with created documents and an error, Home card and banner, cash forecast.
- [x] Commit: `Recurring bill and journal entry screens, Home card, and cash forecast`

## Task 6: Statement of cash flows
Interfaces:
```python
# ledger/cash_flow.py
@dataclass class CashFlowLine: account, amount
@dataclass class CashFlow: start, end, net_income, operating, investing, financing, total_operating,
                           total_investing, total_financing, net_change, cash_start, cash_end, bank_change, difference
def cash_flow(start, end) -> CashFlow
```
- [ ] Classification by subtype (spec 4). Use `finance:financial-statements`; record findings in the commit body.
- [ ] View, template, CSV, PDF, catalog (Business overview), featured list unchanged.
- [ ] Tests `tests/test_cash_flow.py`: each section, signs, Difference 0.00 on a ledger with every subtype, empty range.
- [ ] Screenshots: the report, PDF.
- [ ] Commit: `Statement of cash flows`

## Task 7: Bad debt write-off
Interfaces:
```python
# sales/models.py
SalesSettings.bad_debt_account: FK Account, null=True, blank=True
CreditNote.write_off_of: FK Invoice, null=True, blank=True, related_name="write_offs"
# sales/posting/writeoff.py
def write_off(invoice, *, date, amount, reason, user) -> CreditNote
```
- [ ] Settings field and form (expense accounts only).
- [ ] `write_off`: admin only, validations (spec 5.5), one transaction: credit note + application.
- [ ] Refuse refunds from, and applying to another invoice, a write-off (spec 5.4).
- [ ] Cash basis: exclude write-off entries and write-off applications.
- [ ] Sales by customer: Written off column.
- [ ] Invoice page: Write off button (admin, balance > 0, not void); form; credit note page says "Write-off of <invoice>".
- [ ] Tests `tests/test_write_off.py`: ledger, balance, aging, statement, profit, cash basis, refusals, undo by removing then voiding.
- [ ] Screenshots: invoice with Write off, the form and an error, the invoice after, the credit note, Sales settings.
- [ ] Commit: `Bad debt write-off`

## Task 8: Docs and PR
- [ ] README: What it does rows and Status line.
- [ ] CLAUDE.md: add this spec and plan to Source of truth.
- [ ] Full `uv run pytest -q`.
- [ ] Commit: `Docs: attachments, new reports, recurring, cash flow, write-off`; open the PR.

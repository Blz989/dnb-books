# Phase 5b: Zoho Migration and Catch-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:**
- An `import_zoho` command that replays the owner's whole Zoho history through DNB Books' own posting functions and proves the result with a tie-out report.
- Two inbox actions, **Pay bills** and **Receive payment**, plus a Plaid feed start date, for catching up 2026 from the bank.

**Architecture:**
- **New `migration` app.**
  - `zoho/reader.py` turns the export zip into plain row dicts.
  - `zoho/mapping.py` turns rows into the existing spec objects, as pure functions.
  - `zoho/validate.py` finds every bad reference before anything posts.
  - `zoho/replay.py` posts day by day through the existing `sales`, `purchasing`, `items`, and `ledger` functions.
  - `tieout.py` compares the result with Zoho's reports.
- **The command always imports into a copy of the database** and swaps it in only after a full, successful real run.
- **Catch-up actions** live in `banking/inbox.py` beside the Phase 5a actions.

**Tech Stack:** As in previous phases. No new dependencies. Use only the stdlib `csv`, `zipfile`, and `sqlite3`.

**Spec:** `docs/superpowers/specs/2026-10-28-phase-5b-zoho-migration-design.md`.

## Global Constraints

- **`CLAUDE.md` applies in full.** Branch `claude/phase-5b-zoho-migration`. PR title `Phase 5b: Zoho migration and catch-up`.
- **The owner's real Zoho export is not available to the build session and must never be committed.** Build and test against the made-up sample export from Task 1. After merge, Claude runs the dry runs against the real export in the owner's session and fixes any mapping gaps in a follow-up branch. Say this in the PR.
- **Every posted amount goes through an existing posting function** (`create_order`, `create_invoice`, `create_po`, `create_bill`, `receive_payment`, `apply_payment`, `make_deposit`, `pay_vendor`, `create_credit_note`, `apply_credit`, `create_allowance_credit`, `create_return_credit`, `apply_vendor_credit`, `record_movements`, `record_opening_batch`, `record_value_adjustment`, `post_entry`). The importer never writes `JournalEntry` or `JournalLine` rows itself.
- **Import user:** the command takes `--user <username>` (an admin) and posts as that user.
- **Allowances during import:**
  - `allow_negative=True` on every stock-affecting call.
  - `allow_duplicate=True` on bills.
  - Zoho dates go straight through, and every month stays open.
- **Money:** Zoho amounts are parsed with `Decimal`, after stripping commas. Zoho writes `0.000` and `0.0`, and those parse fine.
- **Journal `source_type`s:** `migration.expense`, `migration.journal`, `migration.transfer`, `migration.deposit`, and `migration.opening`, each with `source_id` = the Zoho ID.
- **Exact strings:**

  | Where | Message |
  |---|---|
  | Non-empty database | `The database already has documents. Import only into a new, empty database.` |
  | Missing column | `{file}: column "{column}" is missing.` |
  | Bad reference | `{file} row {n}: {kind} "{value}" is not in the export.` |
  | Duplicate number | `{file} row {n}: number {number} appears twice.` |
  | Number used | `{number} is already used.` |
  | Feed start | `Before the feed start date.` |
  | Pay bills mismatch | `The bills you chose add up to {x}. The bank shows {y}.` |
  | Receive payment mismatch | `The invoices you chose add up to {x}. The bank shows {y}.` |
  | No balance | `Enter the statement balance.` |

  `{x}` and `{y}` are formatted `1,234.56`.

## Review Focus

1. **An invoice line whose Zoho `Item Total` doesn't equal quantity × price** (a discount; 54,867 of 102,388 real lines). The line uses `price = Item Total / Quantity` to 4 dp. Any cent left over after rounding goes on the invoice's "Adjustment" line, so the invoice total equals Zoho's `Total` exactly. The same applies to bills. Pinned in Task 2 and Task 5.
2. **Payment numbers in mixed formats** (`1234`, `CP-1234`, `123`): each keeps its exact Zoho number. Our own sequences continue without clashing. Pinned in Task 4 and Task 9.
3. **A failure halfway through the real run:** the real database file is untouched, with no documents and no leftover `-wal` or `-shm` files from the copy. Pinned in Task 9.
4. **A bank row dated before `feed_starts` that is also a cross-source duplicate:** it is excluded with the feed reason, never the duplicate reason, and is counted once. Pinned in Task 10.
5. **Pay bills on a row with a check number already used on that account:** refused with the Phase 5a check-number message, and nothing is created. Pinned in Task 11.

---

## File Structure

```
sales/numbering.py                 + use_number(doc_type, number) context manager
banking/models.py                  BankAccount.feed_starts; Reconciliation.statement_balance nullable
banking/importers.py               feed_starts rule
banking/plaid_client.py            days_requested=365
banking/reconcile.py               finish/difference with a blank balance; set_statement_balance
banking/inbox.py                   pay_bills_from_row, receive_payment_from_row
banking/views/inbox.py, views/reconcile.py, forms.py, templates/banking/*   screens
migration/
  apps.py  models.py (ZohoRecord, ImportRun)  admin.py (ImportRun read-only)
  zoho/reader.py  zoho/mapping.py  zoho/validate.py  zoho/replay.py
  tieout.py  templates/migration/tieout.html  static/migration/tieout.css
  management/commands/import_zoho.py
tests/zoho_sample.py               build_sample_export(tmp_path, **changes) -> Path
tests/ test_zoho_reader.py, test_zoho_mapping.py, test_zoho_validate.py, test_zoho_replay_master.py,
       test_zoho_replay_docs.py, test_zoho_replay_money.py, test_zoho_after.py, test_zoho_tieout.py,
       test_import_zoho_command.py, test_feed_starts.py, test_inbox_catchup.py, test_inbox_catchup_views.py,
       test_phase5b_acceptance.py
```

---

### Task 1: Migration app, reader, and the sample export

**Files:**
- Create: `migration/__init__.py`, `apps.py`, `models.py`, `admin.py`, `migrations/0001_initial.py`, `migration/zoho/__init__.py`, `migration/zoho/reader.py`, `tests/zoho_sample.py`
- Modify: `dnb/settings.py` (`INSTALLED_APPS`)
- Test: `tests/test_zoho_reader.py`

**Interfaces:**
- Produces:
  - `ZohoRecord(kind: str, zoho_id: str, content_type, object_id)`, with `unique_together = (kind, zoho_id)`.
  - `ImportRun(started_at, finished_at, dry_run: bool, export_name, user, summary: JSONField, report_pdf: FileField(upload_to="migration/"), succeeded: bool)`.
  - `ZohoExport`, a dataclass of `files: dict[str, list[dict[str, str]]]`, keyed by short name.
  - `read_export(path: Path) -> ZohoExport`.
  - `class ExportError(Exception)` with `.problems: list[str]`.
  - `REQUIRED: dict[str, tuple[str, ...]]` (below).
  - `tests/zoho_sample.py: build_sample_export(tmp_path, *, drop_column: tuple[str, str] | None = None, extra_rows: dict[str, list[dict]] | None = None) -> Path`.

The export layout, taken from the real file:
- Top-level CSVs.
- `Invoice.zip` containing `Invoice00.csv` … `Invoice04.csv`.
- `Sales_Order.zip` containing `Sales_Order00.csv` … `Sales_Order04.csv`.

Files in the nested zips are read in name order and joined. Each document is one row per line, grouped by its ID column. Encoding is `utf-8-sig`. Raise `csv.field_size_limit` to 10**9.

`REQUIRED` maps short name to file and columns. Only these columns are read:

| Short name | File | ID column | Columns read |
|---|---|---|---|
| accounts | `Chart_of_Accounts.csv` | Account ID | Account Name, Account Type, Account Status, Description |
| items | `Item.csv` | Item ID | Item Name, SKU, Description, Rate, Purchase Rate, Account, Purchase Account, Inventory Account, Item Type, Status, Usage unit, Category Name, Vendor, CF.Vendor SKU, Opening Stock, Opening Stock Value |
| customers | `Contacts.csv` | Contact ID | Display Name, Company Name, EmailID, Phone, Billing Address, Billing Street2, Billing City, Billing State, Billing Code, Shipping Address, Shipping Street2, Shipping City, Shipping State, Shipping Code, Payment Terms Label, Status, Notes |
| vendors | `Vendors.csv` | Contact ID | Display Name, EmailID, Phone, Billing Address, Billing City, Billing State, Billing Code, Payment Terms Label, Status |
| sales_orders | `Sales_Order.zip` | SalesOrder ID | Order Date, SalesOrder Number, Status, Customer Name, Item Name, SKU, Item Desc, QuantityOrdered, Item Price, Item Total, Total, Shipping Charge, Adjustment, Notes |
| invoices | `Invoice.zip` | Invoice ID | Invoice Date, Invoice Number, Invoice Status, Customer Name, Sales Order Number, Item Name, SKU, Item Desc, Quantity, Item Price, Item Total, Total, Shipping Charge, Shipping Charge Account, Adjustment, Adjustment Account, Item Tax Amount, Notes, Payment Terms Label |
| purchase_orders | `Purchase_Order.csv` | Purchase Order ID | Purchase Order Date, Purchase Order Number, Vendor Name, Item Name, SKU, QuantityOrdered, Item Price, Item Total |
| bills | `Bill.csv` | Bill ID | Bill Date, Bill Number, Vendor Name, Bill Status, Item Name, SKU, Account, Description, Quantity, Rate, Item Total, Total, Adjustment, Adjustment Account, Payment Terms Label, Purchase Order Number, CF.Sales Order |
| customer_payments | `Customer_Payment.csv` | CustomerPayment ID | Payment Number, Mode, Date, Amount, Unused Amount, Bank Charges, Reference Number, Customer Name, Deposit To, Invoice Number, Amount Applied to Invoice, Invoice Payment Applied Date |
| vendor_payments | `Vendor_Payment.csv` | VendorPayment ID | Payment Number, Mode, Date, Amount, Reference Number, Vendor Name, Paid Through, Bill Number, Bill Amount, Bill Payment Applied Date |
| credit_notes | `Credit_Note.csv` | CreditNotes ID | Credit Note Date, Credit Note Number, Customer Name, Item Name, Account, Item Total, Total, Reason |
| credit_applications | `Creditnotes_Invoice.csv` | — | Credit Note Number, Invoice Number, Amount, Date |
| vendor_credits | `Vendor_Credits.csv` | VendorCredit ID | Vendor Credit Date, Vendor Credit Number, Vendor Name, Item Name, SKU, Quantity, Account, Item Total, Total, Bill Number, Amount Applied |
| expenses | `Expense.csv` | Expense Reference ID | Expense Date, Expense Description, Expense Account, Paid Through, Expense Amount, Total, Reference#, Vendor |
| journals | `Journal.csv` | Journal ID | Journal Date, Journal Number, Notes, Account, Debit, Credit, Description |
| deposits | `Deposit.csv` | — | Transaction Date, Transaction Type, From Account, To Account, Reference, Description, Total |
| transfers | `Transfer_Fund.csv` | — | Transaction Date, Transaction Type, From Account, To Account, Reference, Description, Total |
| adjustments | `Inventory_Adjustment.csv` | InventoryAdjustment ID | Date, Adjustment Type, Reason, Description, Item Name, SKU, Account, Quantity Adjusted, Value Adjusted |

The column names for `Credit_Note.csv`, `Creditnotes_Invoice.csv`, and `Vendor_Credits.csv` are not confirmed against the real file; the others are. If a real header differs, Claude's dry run fixes `REQUIRED` in the follow-up. Rows of `deposits` and `transfers` have no ID column. Their ID is `"{file}:{row number}"`.

**The sample export.** `tests/zoho_sample.py` writes CSVs with exactly the `REQUIRED` columns, plus nested zips. The content:
- 6 accounts beyond the core ones: Bank `BUS COMPLETE CHK`, Credit Card `Chase Ink Unlimited`, Credit Card `Line Of Credit - Chase`, Other Liability `Line Of Credit - Headway`, Expense `Shipping Expenses`, Income `Shipping Charge`
- 4 stock items, one of them with opening stock
- 1 non-stock item
- 3 customers
- 3 vendors, one being "Mr. Joseph Cordon"
- 2024–2026 activity: 8 sales orders (one void), 9 invoices (one with a discount line, one with a shipping charge, one with an adjustment, one with an item tax amount, and two shipped against the same order), 3 purchase orders, 5 bills (one Cordon "Shipping for Order SO-…" bill, one with `CF.Sales Order`)
- customer payments: some to `Undeposited Funds` (Mode Cash and Check), some to `BUS COMPLETE CHK` with numbers `1001`, `CP-1002`, and `57`; one with Unused Amount; one with Bank Charges
- 2 vendor payments (Check with reference, Bank Transfer); 1 credit note with an application; 1 vendor credit applied to a bill
- 2 expenses, 1 journal, 3 transfers (one `Card Payment`, one from `Undeposited Funds` whose amount equals exactly two cash payments, one `Funds Transfer` from `Line Of Credit - Chase`), 2 other deposits (`Capital Introduced`, `Expense Refund`), and 2 inventory adjustments (quantity, value)
- one item that goes negative

- [x] **Step 1: Write the failing tests** in `tests/test_zoho_reader.py`:
  - `test_reads_nested_zips_in_name_order`: `len(export.files["invoices"])` equals the line count the sample wrote across the two inner files.
  - `test_strips_bom_and_reads_quoted_commas`: an Item Desc containing `"Box, 12 ct"` reads intact.
  - `test_missing_column_reports_file_and_column`: with `drop_column=("bills", "Rate")`, it raises `ExportError`, and `problems == ['Bill.csv: column "Rate" is missing.']`.
  - `test_missing_file_is_empty_list_except_core`: an absent `Vendor_Credits.csv` reads as `[]`. An absent `Chart_of_Accounts.csv` raises `Chart_of_Accounts.csv: column "Account Name" is missing.`
  - `test_models_unique_zoho_record`
- [x] **Step 2:** Run `uv run pytest tests/test_zoho_reader.py -v`. Expect FAIL.
- [x] **Step 3:** Implement the models, migration, read-only admin, reader, and sample builder.
- [x] **Step 4:** Run `uv run pytest tests/test_zoho_reader.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: migration app and Zoho export reader`.

### Task 2: Mapping (pure functions)

**Files:**
- Create: `migration/zoho/mapping.py`
- Test: `tests/test_zoho_mapping.py`

**Interfaces:**
- Produces:
  - `money(text: str) -> Decimal`: strips commas, blank gives `0.00`, quantized to 2 dp.
  - `account_plan(rows: list[dict]) -> list[AccountPlan]`, where `AccountPlan(name, number: str, type, subtype, is_active)`. It follows spec 4.1's table: steps of 10, ordered by name within each range, Accounts Receivable fixed at `1100` and Accounts Payable at `2000`. Expense uses 6000–7999. A range that overflows raises `ValueError`.
  - `bank_kind(name: str, zoho_type: str) -> AccountKind | None`:
    - `Bank` (or a `Cash` type named other than "Undeposited Funds" or "Petty Cash"): CHECKING.
    - `Petty Cash`: None.
    - Credit Card named `Line Of Credit…`: LOAN.
    - Other Credit Card: CARD.
    - Other Liability named `Line Of Credit…`: LOAN.
    - Anything else: None.
  - `terms(label: str) -> Terms`: `Due on Receipt` or `Due On Receipt` → DUE_ON_RECEIPT. `Net N` → the matching `NET_N` if it exists, else the next longer one. `Due end of the month` → NET_30. Blank → NET_30.
  - `address(row, prefix: str) -> str`: joins Address, Street2, and the City, State, Code line, dropping blanks.
  - `line_price(total: Decimal, quantity: int) -> tuple[Decimal, Decimal]`: returns `(price_4dp, leftover)`, where `leftover = total − round_half_up(quantity × price, 2)` (Review Focus 1).
  - `customer_method(mode: str, deposit_to: str) -> PaymentMethod`: deposit to `Undeposited Funds` gives CHECK when Mode is `Check`, otherwise CASH. Any other `Deposit To` gives ACH.
  - `vendor_method(mode: str, paid_through_subtype) -> VendorPaymentMethod`: a credit card subtype gives CARD. Mode `Check` gives CHECK. Anything else gives ACH. The Zoho Mode is kept in the reference.

- [x] **Step 1: Write the failing tests** in `tests/test_zoho_mapping.py`:
  - `test_money_parses_zoho_forms`: `"1,234.5"` → `Decimal("1234.50")`, `"0.000"` → `Decimal("0.00")`, `""` → `Decimal("0.00")`.
  - `test_account_plan_numbers_by_type_and_name`: two Bank accounts get `1000` and `1010` in name order. A/R gets `1100`, A/P `2000`, Credit Card `2100`, Income `4000`, Expense `6000`. Inactive stays inactive.
  - `test_bank_kind` (each case above)
  - `test_terms_labels`
  - `test_line_price_discount`: `line_price(Decimal("10.00"), 3) == (Decimal("3.3333"), Decimal("0.00"))`, `line_price(Decimal("1000.00"), 300) == (Decimal("3.3333"), Decimal("0.01"))`, and `line_price(Decimal("0.00"), 2) == (Decimal("0.0000"), Decimal("0.00"))`.
  - `test_customer_method`
  - `test_vendor_method`
- [x] **Step 2:** Run `uv run pytest tests/test_zoho_mapping.py -v`. Expect FAIL.
- [x] **Step 3:** Implement.
- [x] **Step 4:** Run `uv run pytest tests/test_zoho_mapping.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: Zoho field mapping`.

### Task 3: Validation

**Files:**
- Create: `migration/zoho/validate.py`
- Test: `tests/test_zoho_validate.py`

**Interfaces:**
- Consumes: `ZohoExport`.
- Produces: `validate(export: ZohoExport) -> list[str]`, the problem messages from Global Constraints. Rows are numbered as file line numbers (header = 1). Within nested zips, the number is `"{inner file} row {n}"`.

Checks:
- Every customer name on sales orders, invoices, payments, and credit notes is in `customers`.
- Every vendor name on bills, POs, vendor payments, and vendor credits is in `vendors`.
- Every SKU or Item Name on document lines is in `items`. Match by SKU, falling back to Item Name when the SKU is blank. Lines with no item and an `Account` (bills, credit notes) are expense lines and are fine.
- Every account name used (`Account`, `Paid Through`, `Deposit To`, `Expense Account`, `From Account`, `To Account`, `Adjustment Account`, `Shipping Charge Account`) is in `accounts`.
- Every `Invoice Number` on payment and credit applications is in `invoices`.
- Every `Bill Number` on vendor payment and credit applications is a bill of the same vendor.
- `Sales Order Number` on invoices is in `sales_orders`.
- No duplicate `SalesOrder Number`, `Invoice Number`, Customer `Payment Number`, Vendor `Payment Number`, `Credit Note Number`, or `Purchase Order Number`.

- [x] **Step 1: Write the failing tests:**
  - `test_clean_sample_has_no_problems`
  - `test_unknown_customer`: `extra_rows` adds an invoice for "Nobody Inc". The problem is `Invoice00.csv row {n}: customer "Nobody Inc" is not in the export.`
  - `test_unknown_item_sku_then_name`
  - `test_unknown_account`
  - `test_unknown_invoice_on_payment`
  - `test_bill_of_other_vendor_on_payment`
  - `test_duplicate_invoice_number`
  - `test_all_problems_listed_at_once` (three injected, three returned)
- [x] **Step 2:** Run `uv run pytest tests/test_zoho_validate.py -v`. Expect FAIL.
- [x] **Step 3:** Implement.
- [x] **Step 4:** Run `uv run pytest tests/test_zoho_validate.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: Zoho export validation`.

### Task 4: Replay master data and keeping Zoho numbers

**Files:**
- Create: `migration/zoho/replay.py` (master data part)
- Modify: `sales/numbering.py` (`use_number`)
- Test: `tests/test_zoho_replay_master.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces:
  - In `sales/numbering.py`, a context manager `use_number(doc_type: DocType, number: str)`. While it is active, the next `next_number(doc_type)` call returns `number` exactly, without touching the sequence. It raises `SalesError("{number} is already used.")` if the model already has it. The override is consumed by one call, is thread-local, and is cleared on exit.
  - `class Replay`: `Replay(export: ZohoExport, user)`, with these attributes:
    - `accounts: dict[str, Account]` (by Zoho name)
    - `items: dict[str, Item]` (by SKU, and by name where the SKU is blank)
    - `customers: dict[str, Customer]` and `vendors: dict[str, Vendor]` (by name)
    - `warnings: list[str]`
  - `Replay.load_master() -> None`, which:
    - creates the accounts from `account_plan`
    - creates the non-stock items `SHIP` "Shipping charge" (income: Zoho "Shipping Charge" account), `ADJ` "Adjustment" (income: Zoho "Sales"), and `TAX` "Sales tax (Zoho)" (income: Zoho "Tax Payable")
    - fills in `InventorySettings`:
      - inventory: "Inventory Asset"
      - COGS: "Cost of Goods Sold"
      - income: "Sales"
      - shrinkage and writedown: "Cost of Goods Sold"
      - opening equity: "Opening Balance Offset"
    - fills in `SalesSettings` (A/R: "Accounts Receivable", Undeposited: "Undeposited Funds", returns: "Sales") and `PurchasingSettings` (A/P: "Accounts Payable")
    - creates a `BankAccount` per `bank_kind` with `feed_starts=date(2026,1,1)`
    - creates the items, customers, and vendors. "Mr. Joseph Cordon" is created as an ordinary vendor; handling settings are left alone.
    - posts opening stock with `record_opening_batch(date=date(2020,2,1), ...)`, unit cost = `Opening Stock Value / Opening Stock`
    - creates a `ZohoRecord` for every object
  - `Replay.record(kind: str, zoho_id: str, obj) -> None`.

- [x] **Step 1: Write the failing tests:**
  - `test_use_number_forces_one_number`: inside `use_number(DocType.PMT, "CP-1002")`, `next_number(DocType.PMT) == "CP-1002"`, and the next call falls back to the sequence (`PMT-00001`).
  - `test_use_number_refuses_used` (`{number} is already used.`)
  - `test_master_accounts_numbered_and_settings_configured`: all three settings report `is_configured`.
  - `test_bank_accounts_created_by_kind`: `BUS COMPLETE CHK` CHECKING, `Chase Ink Unlimited` CARD, `Line Of Credit - Chase` LOAN, `Line Of Credit - Headway` LOAN, and none for Petty Cash or Undeposited Funds.
  - `test_items_customers_vendors_and_zoho_records`
  - `test_opening_stock_posted_at_2020_02_01`
- [x] **Step 2:** Run `uv run pytest tests/test_zoho_replay_master.py -v`. Expect FAIL.
- [x] **Step 3:** Implement.
- [x] **Step 4:** Run `uv run pytest tests/test_zoho_replay_master.py tests/test_numbering.py -v`. Expect PASS.
- [x] **Step 5:** Commit with `feat: replay Zoho master data; keep Zoho numbers`.

### Task 5: Replay orders, purchase orders, bills, and invoices

Use the `finance:journal-entry` skill to review the invoice and bill postings for discounts, shipping, adjustments, and tax lines. Record the findings in the commit body.

**Files:**
- Modify: `migration/zoho/replay.py`
- Test: `tests/test_zoho_replay_docs.py`

**Interfaces:**
- Produces:
  - `Replay.events() -> list[Event]`, where `Event(date, rank: int, number: str, kind: str, zoho_id: str)`, sorted by `(date, rank, number)`. Ranks follow spec 4.3:
    - 1: sales orders and POs
    - 2: bills, and adjustments with quantity > 0 or value > 0
    - 3: invoices
    - 4: payments, credits, and applications
    - 5: Undeposited transfers
    - 6: expenses, journals, other transfers, and other deposits
    - 7: adjustments that remove stock
  - `Replay.post(event: Event) -> None`, which dispatches on `kind`. This task handles `sales_order`, `purchase_order`, `bill`, and `invoice`.

Rules:
- **Sales order:** `create_order` inside `use_number(DocType.SO, number)`, with lines at `line_price(Item Total, QuantityOrdered)`. Status `void` is closed with `close_order` after creation. The plan's reading: Phase 3 has no void for orders, so "comes over void" in spec 4.2 becomes closed, with the note "Void in Zoho".
- **Purchase order:** `create_po` inside `use_number(DocType.PO, number)`, then `close_po`. Numbers without a `PO-` prefix are kept as they are.
- **Bill:** `create_bill(allow_duplicate=True, allow_negative=True)`, with `vendor_invoice_number = Bill Number` and our own `BILL-` number.
  - Item lines are `BillItemSpec(item, qty, line_price(...)[0])`.
  - Lines with no item are `BillExpenseSpec(account, Item Total, Description)`.
  - Leftover cents and a nonzero `Adjustment` become a `BillExpenseSpec` on `Adjustment Account`, or else on "Cost of Goods Sold".
  - Notes: `Imported from Zoho (ID {id})`, plus `Sales order {CF.Sales Order}` when that is set.
  - The terms come from `terms()`.
- **Invoice:** `create_invoice(sales_order=..., allow_negative=True)` inside `use_number(DocType.INV, number)`.
  - Lines carry `so_line` = the order's line with the same item, the first one with remaining quantity. Fulfillment is STOCK, because Zoho tracked all of these as stock.
  - Extra lines:
    - `SHIP` at Shipping Charge
    - `ADJ` at `Adjustment` plus every leftover cent
    - `TAX` at the sum of `Item Tax Amount`
  - The invoice must total Zoho's `Total`. If not, record a warning `"{number}: total {ours} vs Zoho {theirs}"` and continue.
- Every document gets a `ZohoRecord`.

- [ ] **Step 1: Write the failing tests:**
  - `test_orders_keep_zoho_numbers_and_void_closed`
  - `test_invoice_totals_equal_zoho` (every sample invoice)
  - `test_discount_leftover_goes_on_adjustment_line` (Review Focus 1)
  - `test_shipping_and_tax_lines`
  - `test_two_invoices_against_one_order`
  - `test_bill_stock_in_at_rate_and_expense_lines`
  - `test_cordon_bill_on_shipping_expenses_with_order_note`
  - `test_negative_stock_allowed_and_warned`
  - `test_event_order_within_a_day`: a bill and an invoice on the same date; the bill posts first.
  - `test_ledger_checks_zero_after_docs`: `ar_check`, `ap_check`, and `check_invariants` are all empty.
- [ ] **Step 2:** Run `uv run pytest tests/test_zoho_replay_docs.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_zoho_replay_docs.py -v`. Expect PASS.
- [ ] **Step 5:** Commit with `feat: replay Zoho orders, bills, and invoices`.

### Task 6: Replay payments, credits, deposits, expenses, journals, transfers, and adjustments

Use the `finance:reconciliation` skill to review that Undeposited Funds and the bank accounts tie after replay. Record the findings in the commit body.

**Files:**
- Modify: `migration/zoho/replay.py`
- Test: `tests/test_zoho_replay_money.py`

**Interfaces:**
- Produces: `Replay.post` handles `customer_payment`, `vendor_payment`, `credit_note`, `credit_application`, `vendor_credit`, `undeposited_transfer`, `expense`, `journal`, `transfer`, `other_deposit`, and `adjustment`. It also provides `Replay.load_opening_balances(path: Path) -> None` and `Replay.load_deposit_pairs(path: Path) -> None`.

Rules:
- **Customer payment:**
  - `receive_payment(method=customer_method(...), amount=Amount, bank_account=` the `Deposit To` account when it isn't Undeposited Funds`)`, inside `use_number(DocType.PMT, Payment Number)`, with no applications.
  - Then `apply_payment` for each row with `Amount Applied to Invoice > 0`, dated `Invoice Payment Applied Date` or else the payment date.
  - `Bank Charges > 0` posts an entry: debit "Bank Fees and Charges", credit the deposit account (`migration.expense`).
- **Vendor payment:** `pay_vendor(method=vendor_method(...), paid_from=Paid Through, check_number=Reference Number when CHECK, applications=[(bill, Bill Amount)])`, inside `use_number(DocType.VP, Payment Number)`. Bills are found through `ZohoRecord` by vendor and Bill Number.
- **Credit notes and vendor credits:**
  - Credit notes: `create_credit_note` with `CreditLineSpec(description, Item Total, account)` inside `use_number(DocType.CN, number)`, then `apply_credit` per `Creditnotes_Invoice.csv` row.
  - Vendor credits: `create_return_credit` when lines have items, otherwise `create_allowance_credit`, then `apply_vendor_credit` where `Bill Number` is set.
- **Undeposited transfer** (a transfer whose From Account is "Undeposited Funds"):
  - Find undeposited payments dated on or before the transfer, oldest first, whose amounts sum exactly to `Total`. Search subsets in date order, at most 20 candidates, with smallest-first pruning.
  - When found, `make_deposit(payments, bank_account=To Account, date)`.
  - Else use `deposit_pairs.csv` (columns `date,amount,payment_numbers`, numbers separated by `;`).
  - Else post nothing and record the warning `Undeposited transfer {date} {amount}: no exact set of payments.` The command fails the run on any such warning (Task 9).
- **Expense:** `post_entry` debit `Expense Account`, credit `Paid Through`, for `Total`.
- **Journal:** one `post_entry` per Journal ID, with lines from Debit and Credit.
- **Transfer** (others) **and other deposit:** `post_entry` debit `To Account`, credit `From Account`, for `Total`.
- **Adjustment:**
  - Quantity: `record_movements(kind=ADJUSTMENT, lines=[StockLine(item, qty, reason, account)], allow_negative=True)`.
  - Value: `record_value_adjustment(item, amount=Value Adjusted, reason, account)`.
- **Opening balances CSV** (`account,debit,credit`): one `post_entry` dated 2020-02-01 (`migration.opening`), balanced against "Opening Balance Offset" if the rows don't balance.

- [ ] **Step 1: Write the failing tests:**
  - `test_payments_to_undeposited_and_to_bank`
  - `test_mixed_payment_numbers_kept` (Review Focus 2): `1001`, `CP-1002`, and `57` exist exactly.
  - `test_unused_amount_stays_unapplied`
  - `test_bank_charges_entry`
  - `test_vendor_payment_check_number_registered_handwritten`
  - `test_credit_note_and_vendor_credit_applied`
  - `test_undeposited_transfer_becomes_deposit`, with `undeposited_check` empty
  - `test_undeposited_transfer_without_exact_set_warns`
  - `test_deposit_pairs_resolve_it`
  - `test_expense_journal_transfer_other_deposit_entries`
  - `test_quantity_and_value_adjustments`
  - `test_opening_balances_csv`
  - `test_all_ledger_checks_zero_after_full_replay`: after every event, `ar_check`, `ap_check`, `undeposited_check`, and `check_invariants` are empty.
- [ ] **Step 2:** Run `uv run pytest tests/test_zoho_replay_money.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_zoho_replay_money.py -v`. Expect PASS.
- [ ] **Step 5:** Commit with `feat: replay Zoho payments, credits, deposits, and entries`.

### Task 7: After the replay: Dec 31 reconciliations, sequences, and lists

**Files:**
- Modify:
  - `banking/models.py`: `Reconciliation.statement_balance` becomes `null=True`, with a migration.
  - `banking/reconcile.py`: `difference()` returns `None` when the balance is `None`; `finish` refuses with `Enter the statement balance.`; add `set_statement_balance(rec, balance: Decimal, user) -> None`, which works only while IN_PROGRESS.
  - `banking/views/reconcile.py` and `templates/banking/reconcile_work.html`: an editable "Statement balance" field on the working page; the difference shows `—` when blank.
  - `migration/zoho/replay.py`
- Test: `tests/test_zoho_after.py`

**Interfaces:**
- Produces:
  - `Replay.finish() -> AfterSummary`, which does three things:
    - For each `BankAccount` whose ledger account has lines dated on or before 2025-12-31, it creates a `Reconciliation(statement_date=2025-12-31, statement_balance=None, IN_PROGRESS)` with every line through that date as a ticked item.
    - It sets the SO, INV, CN, and PO sequences with `set_sequence(prefix, next_number=max+1)`, from the highest number with each Zoho prefix (`SO-`, `INV-`, `CN-`, `PO-`). PMT, VP, VC, and BILL keep our own prefixes; `next_number` already skips used numbers.
    - It returns the lists for the report.
  - `AfterSummary(unbilled_orders: list[(SalesOrder, profit, at_50, at_55)], undeposited_by_year: dict[int, (count, total)], negative_items: list[(Item, on_hand)])`.
    - `unbilled_orders`: sales orders dated 2024-03-29 or later that have no Cordon bill note `Sales order {number}`. Profit comes from `handling.profit.order_profit(key, as_of=today)`.

- [ ] **Step 1: Write the failing tests:**
  - `test_dec31_reconciliation_created_in_progress_all_ticked_blank_balance`
  - `test_finish_requires_statement_balance`
  - `test_set_statement_balance_then_finish_at_zero`
  - `test_sequences_continue_after_zoho_numbers`: the next INV is the highest + 1, and the next PMT is `PMT-00001`.
  - `test_unbilled_orders_listed`
  - `test_undeposited_by_year`
  - `test_existing_reconciliations_unaffected` (run the Phase 5a reconcile tests)
- [ ] **Step 2:** Run `uv run pytest tests/test_zoho_after.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_zoho_after.py tests/test_reconcile.py tests/test_reconcile_views.py -v`. Expect PASS.
- [ ] **Step 5: Screenshots.** The working page with a blank statement balance (difference `—`), and after typing the balance. Light and dark. Send them.
- [ ] **Step 6:** Commit with `feat: Dec 31 reconciliations, sequences, and post-import lists`.

### Task 8: Tie-out report

**Files:**
- Create: `migration/tieout.py`, `migration/templates/migration/tieout.html`, `migration/static/migration/tieout.css`
- Test: `tests/test_zoho_tieout.py`

**Interfaces:**
- Consumes: the ledger (`JournalLine.objects.in_ledger()`), `sales.reports.ar_aging`, `purchasing.reports.ap_aging`, `items.stock.on_hand` and `stock_value`, and `AfterSummary`.
- Produces:
  - `Check(name, passed: bool, rows: list[Diff])` and `Diff(label, ours: Decimal, zoho: Decimal, note: str = "")`.
  - `TieOut(checks: list[Check], inventory_value: list[Diff], counts: list[Diff], after: AfterSummary, account_map: list[AccountPlan], warnings: list[str])`, with property `passed` (all checks passed).
  - `compare(reports_dir: Path, as_of: date, after: AfterSummary, plan, warnings, export) -> TieOut`.
  - `render_html(t: TieOut) -> str` and `render_pdf(t: TieOut) -> bytes`.

The reports folder holds **normalized CSVs**. During the dry runs, Claude converts the owner's Zoho exports into this form, as spec 6.1 allows conversion. Each file is optional; a missing file makes its check read "Not provided", and counts as failed.

| File | Columns | Check |
|---|---|---|
| `trial_balance.csv` | `account,debit,credit` | Each account's DNB balance (debit − credit, by the mapped account) equals Zoho's (debit − credit). |
| `pl_YYYY.csv` | `account,amount` | Each income and expense account's activity for the year equals Zoho's. Net income is checked too. |
| `ar_aging.csv` / `ap_aging.csv` | `total` | The aging totals are equal. |
| `inventory_summary.csv` | `sku,quantity` | Quantity on hand per SKU is equal. |
| `inventory_valuation.csv` | `sku,value` | Listed in `inventory_value` with the note `ever negative` when the item's on-hand went below zero during replay. Never fails. |

`counts` compares document counts and totals per type with the export: invoices, bills, payments, vendor payments, and credits.

The HTML follows "Ledger paper", in this order:
- the summary (Passed or Failed, plus counts)
- each check with its failing rows first
- inventory value
- negative items
- undeposited by year
- unbilled orders
- warnings
- the account number map

- [ ] **Step 1: Write the failing tests:**
  - `test_matching_reports_pass`: generate the CSVs from the replayed sample's own figures; everything passes.
  - `test_wrong_trial_balance_account_fails_and_is_named`
  - `test_missing_report_not_provided`
  - `test_inventory_value_listed_not_failed`
  - `test_pdf_renders` (`%PDF`)
- [ ] **Step 2:** Run `uv run pytest tests/test_zoho_tieout.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_zoho_tieout.py -v`. Expect PASS.
- [ ] **Step 5: Screenshots.** The tie-out HTML (one passing, one failing) and the PDF page 1. Send them.
- [ ] **Step 6:** Commit with `feat: Zoho tie-out report`.

### Task 9: The import_zoho command

**Files:**
- Create: `migration/management/commands/import_zoho.py`
- Test: `tests/test_import_zoho_command.py`

**Interfaces:**
- Consumes: Tasks 1 to 8, `backup.service.snapshot(dest)`, and `backup.service.run_backup()`.
- Produces: `import_zoho <export.zip> --reports <dir> --user <username> [--dry-run] [--opening-balances <csv>] [--deposit-pairs <csv>]`, which exits 0 when the tie-out passes and 1 otherwise.

Flow:
1. Refuse if any `Invoice`, `Bill`, `Payment`, `VendorPayment`, or `JournalEntry` exists. The command prints the exact message and exits 2.
2. `read_export`, then `validate`. Any problems are printed one per line, and the command exits 2.
3. `snapshot()` the database to `{DNB_DB_PATH}.import-copy`. Then run the replay in a **child process** (`sys.executable manage.py import_zoho --_into-copy ...`, with `DNB_DB_PATH` set to the copy), so Django's connection in the parent never touches the copy. The child does:
   - `load_master`
   - each event in its own `transaction.atomic()`, printing progress once per month (`2021-03: 412 events`)
   - `finish`
   - `compare`
   - saving an `ImportRun` and its report into the copy
   - writing `report.html` and `report.pdf` next to the export
4. **On child failure:** delete the copy and its `-wal` and `-shm` files, print the child's error naming the Zoho file and ID, and exit 1. The real database is untouched (Review Focus 3).
5. **On success with `--dry-run`:** delete the copy and exit by the tie-out result.
6. **On success, real run:**
   - Refuse to continue unless the tie-out passed. Override with `--accept-differences`.
   - `run_backup()`.
   - `os.replace(copy, DNB_DB_PATH)` after deleting the real database's `-wal` and `-shm` files.
   - Print `Imported. Restart the app.`

Document in the README that the app and scheduler are stopped first (`docker compose stop app scheduler`) and the command runs with `docker compose run --rm -u dnb app python manage.py import_zoho ...`.

- [ ] **Step 1: Write the failing tests** (the `file_db_with_entries`-style fixture from `tests/conftest.py` gives a real database file):
  - `test_refuses_non_empty_database`
  - `test_validation_problems_exit_2`
  - `test_dry_run_leaves_database_untouched`: no documents afterwards, and the report files exist.
  - `test_failure_midway_leaves_database_untouched` (Review Focus 3): inject a sample row that makes `post` raise; no documents, and no copy, `-wal`, or `-shm` files remain.
  - `test_real_run_swaps_in_copy_after_backup`: documents exist, a backup file exists, and `ImportRun.succeeded`.
  - `test_real_run_refuses_failed_tieout_without_accept`
  - `test_numbers_after_real_run` (Review Focus 2): next PMT `PMT-00001`, and a new invoice gets the highest INV + 1.
- [ ] **Step 2:** Run `uv run pytest tests/test_import_zoho_command.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_import_zoho_command.py -v`. Expect PASS.
- [ ] **Step 5:** Commit with `feat: import_zoho command with dry run and safe swap`.

### Task 10: Feed start date and Plaid history

**Files:**
- Modify: `banking/models.py` (`BankAccount.feed_starts = DateField(default=date(2026, 1, 1))`, with a migration), `banking/importers.py`, `banking/plaid_client.py`, `banking/forms.py`, `templates/banking/bank_account_form.html`
- Test: `tests/test_feed_starts.py`

**Interfaces:**
- Produces:
  - `import_transactions` saves a row with `date < bank_account.feed_starts` as EXCLUDED with reason `Before the feed start date.`. This check comes before the duplicate check (Review Focus 4). `ImportResult` gains `before_feed_start: int`.
  - `create_link_token` adds `transactions=LinkTokenTransactions(days_requested=365)` when not in update mode.
  - The bank account form shows "Feed starts", with the help text `Bank rows dated before this are set aside.`

- [ ] **Step 1: Write the failing tests:**
  - `test_rows_before_feed_start_excluded_with_reason`
  - `test_feed_start_beats_duplicate_reason` (Review Focus 4): a file row before the feed start that duplicates a Plaid row gets the feed reason and is counted once, in `before_feed_start`.
  - `test_restore_feed_excluded_row`
  - `test_link_token_requests_365_days`: the recorded fake client sees `days_requested == 365`. Update mode sends no `transactions`.
  - `test_bank_account_form_feed_starts`
- [ ] **Step 2:** Run `uv run pytest tests/test_feed_starts.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_feed_starts.py tests/test_imports.py tests/test_plaid_sync.py -v`. Expect PASS.
- [ ] **Step 5: Screenshots.** The bank account form with Feed starts. Send them.
- [ ] **Step 6:** Commit with `feat: feed start date and a year of Plaid history`.

### Task 11: Pay bills and Receive payment from a bank row (services)

**Files:**
- Modify: `banking/inbox.py`
- Test: `tests/test_inbox_catchup.py`

**Interfaces:**
- Consumes: `pay_vendor`, `receive_payment`, `make_deposit`, `inbox.link` (Phase 5a), and `checknumbers.assert_free`.
- Produces:
  - `pay_bills_from_row(txn: BankTransaction, *, vendor: Vendor, amounts: dict[int, Decimal], user) -> VendorPayment`. The keys of `amounts` are bill pks.
    - It is allowed only on a NEW row with amount < 0. Otherwise it raises `InboxError("This bank transaction was already handled.")`, or for the wrong sign, `InboxError("Pay bills is for money going out.")`.
    - The amounts must be positive and add up to `-txn.amount`, or it raises the mismatch message.
    - The method is CHECK when `txn.check_number` is set (passed as `check_number`, so the registry applies, Review Focus 5), CARD when the account kind is CARD, and otherwise ACH.
    - `paid_from` is the row's ledger account, and the date is the row's date.
    - It links the payment to the row. All of this happens in one transaction.
  - `receive_payment_from_row(txn, *, customer: Customer, amounts: dict[int, Decimal], method: PaymentMethod, user) -> Payment`.
    - It is allowed only on a NEW row with amount > 0, on a CHECKING or SAVINGS account. Otherwise it raises `InboxError("Receive payment is for money coming in.")`.
    - The amounts must add up to `txn.amount`.
    - ACH or WIRE: `receive_payment(bank_account=row ledger)`, then link the payment.
    - CHECK: `receive_payment(method=CHECK)`, then `make_deposit([payment], bank_account=row ledger, date=txn.date)`, then link the deposit.
  - `fill_oldest_first(open_docs: list[tuple[int, Decimal]], total: Decimal) -> dict[int, Decimal]`, a pure function used by the views.

- [ ] **Step 1: Write the failing tests:**
  - `test_pay_bills_exact_total_required` (mismatch message with `1,234.56` formatting)
  - `test_pay_bills_check_number_used`
  - `test_pay_bills_card_and_ach`
  - `test_pay_bills_check_number_already_used_refused` (Review Focus 5): pay one bill with check `2050`; a second row with `2050` is refused with the Phase 5a message; no new payment; the row is still NEW.
  - `test_pay_bills_links_and_void_returns_row`
  - `test_pay_bills_wrong_sign`
  - `test_receive_payment_ach`
  - `test_receive_payment_check_makes_one_payment_deposit`: the deposit is matched, and `undeposited_check` is empty.
  - `test_receive_payment_total_mismatch`
  - `test_fill_oldest_first`
  - `test_ledger_checks_zero`
- [ ] **Step 2:** Run `uv run pytest tests/test_inbox_catchup.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_inbox_catchup.py tests/test_inbox.py -v`. Expect PASS.
- [ ] **Step 5:** Commit with `feat: pay bills and receive payment from a bank row`.

### Task 12: Catch-up screens

**Files:**
- Modify: `banking/views/inbox.py`, `banking/urls.py`, `banking/templates/banking/inbox.html`
- Create: `banking/templates/banking/inbox_pay_bills.html`, `inbox_receive_payment.html`
- Test: `tests/test_inbox_catchup_views.py`

**Interfaces:**
- Produces these URLs:
  - `banking:inbox-pay-bills` (`/banking/inbox/<pk>/pay-bills/`)
  - `banking:inbox-receive` (`/banking/inbox/<pk>/receive/`)

  Each one: GET shows the form. `?vendor=` or `?customer=` lists the open documents. POST saves with a one-time token.
- **Inbox row:** "Pay bills" shows on money-out rows and "Receive payment" on money-in rows, next to the Phase 5a actions.
- **Form:**
  - the row summary (date, description, amount)
  - the party select
  - the open documents oldest first: number, date, vendor invoice number or invoice date, balance, and an amount input
  - a "Fill oldest first" button, which uses a GET param `fill=1` and `fill_oldest_first`
  - a live total vs. the bank amount (server-rendered after the fill; a small script updates the total as you type)
  - Method (receive only: ACH, Wire, Check)
  - Save
- Errors show in the banner and the inputs are kept.

- [ ] **Step 1: Write the failing tests:**
  - `test_pay_bills_flow`
  - `test_receive_payment_flow_check`
  - `test_mismatch_keeps_inputs`
  - `test_double_post_same_token_one_payment`
  - `test_actions_shown_by_sign`
  - `test_standard_user_can_use_both`
- [ ] **Step 2:** Run `uv run pytest tests/test_inbox_catchup_views.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement. Match "Ledger paper".
- [ ] **Step 4:** Run `uv run pytest tests/test_inbox_catchup_views.py -v`. Expect PASS.
- [ ] **Step 5: Screenshots** in light and dark:
  - the inbox with both actions
  - Pay bills: empty, then a vendor chosen, then filled, then the mismatch error
  - Receive payment with Check chosen

  Send each.
- [ ] **Step 6:** Commit with `feat: catch-up screens in the bank inbox`.

### Task 13: Acceptance, README, and PR

**Files:**
- Create: `tests/test_phase5b_acceptance.py`
- Modify: `README.md` (the spec section 10 catch-up guide, the import commands from Task 9, and the Status line), `CLAUDE.md` (Done list)

- [ ] **Step 1: Write `test_import_and_catch_up`:**
  - Build the sample and generate matching normalized reports.
  - Run `import_zoho --dry-run` and confirm exit 0, then the real run.
  - Finish the Dec 31 reconciliation after setting its balance.
  - Import a statement CSV for Jan–Feb 2026 into `BUS COMPLETE CHK`, with one row before the feed start.
  - Use Pay bills on one row and Receive payment (check) on another, and book a fee by rule.
  - Reconcile January 2026 at 0.00.
  - Every ledger check stays at 0.00 throughout.
- [ ] **Step 2:** Run `uv run pytest tests/test_phase5b_acceptance.py -v`. Expect PASS.
- [ ] **Step 3:** Run `uv run pytest -q`. Every test passes.
- [ ] **Step 4:** Commit with `test: phase 5b acceptance; docs`.
- [ ] **Step 5:** Open the PR `Phase 5b: Zoho migration and catch-up`. The body follows `CLAUDE.md`, and also says:
  - The importer was built against made-up samples only.
  - Claude will run dry runs against the owner's real export in the owner's session, and fix gaps (column names, unexpected values) in a follow-up branch, before the owner runs it on the server.
  - The manual steps: export the Zoho reports (spec 6.1), stop the app, and run the command as the README says.

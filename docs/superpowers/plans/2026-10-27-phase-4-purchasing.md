# Phase 4: Purchasing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendors, internal purchase orders (including drop-ship POs from sales orders), bills that receive stock at the bill price, vendor payments (check, ACH, card), vendor credits (returns and allowances), the A/P check at month close, purchasing reports with a 1099 summary, and the screens for all of it.

**Architecture:**
- **New `purchasing` app;** `Vendor` lives in `contacts`.
- **One way in:** all postings go through `purchasing/posting/` (`orders.py`, `bills.py`, `payments.py`, `credits.py`), which calls `ledger.posting` and `items.stock`.
- **Drop-ship matching is derived, not stored by hand:** `purchasing/dropship.py` rebuilds the allocations for a sales order line from scratch whenever a bill or invoice touching it changes.
- **Hooks keep `sales` from importing `purchasing`:** sales announces invoice changes through a small hook list.

**Tech Stack:** Unchanged from Phase 3. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-phase-4-purchasing-design.md`. Visual reference: `docs/design/items-mockup.html`.

## Global Constraints

- **`CLAUDE.md` applies in full,** including the screenshot rule and the PR body requirements (task list, test output, deviations, screenshots, manual checks).
- **Money:** `Decimal`. Prices are 4 dp, amounts 2 dp, line amount = `round_half_up(quantity × price, 0.01)`. Quantities are whole numbers.
- **Journal `source_type` values:** `purchasing.bill`, `purchasing.payment`, `purchasing.vendor_credit`. Source id is the document number.
- **Stock movements:** for bills and returns use `MovementKind.RECEIPT`, with movement `source_type` `purchasing.bill` or `purchasing.vendor_credit` and `source_id` the document number.
- **Same patterns as Phase 3:**
  - A document with total 0.00 posts no entry.
  - Voids are admin only, need a reason, must be dated in an open month, and keep the number.
  - Edit-in-place saves a revision snapshot and uses system voids.
  - As-of rules: documents count from their date until `voided_on`; applications count from `applied_on` until `removed_on`.
- **Purchasing settings missing:** every posting raises `PurchasingSettingsMissing("Set up purchasing accounts first.")`. Screens link to `/purchasing/settings/`.
- **Tax IDs** show as `•••-••-1234` (last 4) to Standard users everywhere, CSV included. Admins see the full value only on the vendor edit form and in the 1099 CSV.
- **Exact strings:** user-facing messages exactly as in the spec or this plan.

## Review Focus

1. **One check number used for two vendors in the same Pay bills batch** (or reused from an earlier payment on the same bank account) → refused with "Check {n} is already used on {account}.", nothing saved. Pinned in Task 6.
2. **Drop-ship vendor name on the sales order that matches no vendor** (typo, extra space, different case) → spaces and case are ignored when matching. If still unmatched, no PO is created and the user is asked to pick or create the vendor: "No vendor named {name}. Choose a vendor for these lines." Pinned in Task 3.
3. **Bill with a stock line while inventory settings are incomplete** → "Set up inventory accounts first.", nothing saved. An expense-only bill still saves. Pinned in Task 4.
4. **Vendor invoice number typed differently** (`inv-88123 ` vs `INV-88123`) → the duplicate warning still triggers. Pinned in Task 4.
5. **A payment dated before the bill, or applied to a bill dated after the payment** → "The date can't be before {number}'s date.", nothing saved. Pinned in Task 6.

---

## File Structure

```
contacts/models.py                 + Vendor, mask_tax_id()
sales/models.py, migration         DocType + PO, BILL, VP, VC; DocumentSequence.doc_type max_length 10
sales/numbering.py                 register_document(doc_type, model, prefix, padding)
sales/hooks.py                     invoice change hooks
sales/posting/invoices.py          call hooks after create, edit, void
purchasing/
  apps.py                          ready(): register numbering, hooks, close check
  models.py                        PurchasingSettings, Threshold1099, PurchaseOrder(+Line), Bill, BillItemLine,
                                   BillExpenseLine, BillRevision, DropShipAllocation, VendorPayment,
                                   BillPaymentApplication, VendorCredit(+Line), VendorCreditApplication
  errors.py, balances.py, dropship.py, checks.py, reports.py, csv_export.py
  posting/__init__.py, orders.py, bills.py, payments.py, credits.py
  forms.py, views/ (vendors.py, orders.py, bills.py, payments.py, credits.py, settings.py, reports.py), urls.py
  templates/purchasing/*, demo.py
tests/ test_vendors.py, test_purchasing_models.py, test_purchase_orders.py, test_bills.py, test_dropship.py,
       test_bill_edit_void.py, test_vendor_payments.py, test_vendor_credits.py, test_ap_check.py,
       test_purchasing_reports.py, test_vendor_views.py, test_po_bill_views.py, test_payment_credit_views.py,
       test_phase4_acceptance.py
```

---

### Task 1: Vendors

**Files:**
- Modify: `contacts/models.py`, migration, `contacts/admin.py`, `core/management/commands/setup_roles.py`, `tests/conftest.py`
- Test: `tests/test_vendors.py`

**Interfaces:**
- Produces:
  - `Vendor` with the spec section 4 fields. It reuses `Terms` and `due_date` from Phase 3. Name uniqueness is case-insensitive: "A vendor named {name} already exists."
  - `default_expense_account` must be type EXPENSE: "Choose an expense account."
  - `tax_id` is stored as entered.
  - `mask_tax_id(value: str) -> str` returns `"•••-••-" + last 4 digits`, or `""`.
  - `Vendor.find_by_name(name: str) -> Vendor | None` compares after `strip()`, collapsing inner whitespace and `casefold()`.
  - Registered with auditlog.
- Roles: Standard gets add, change, and view on `contacts.vendor`. `ADMIN_APPS` gains `purchasing`.
- Fixture: `vendor` ("Acme Supply", NET_30, `is_1099=False`).

- [x] **Step 1: Failing tests:**
  - A duplicate name in different case is rejected.
  - `find_by_name("  acme   supply ")` returns the vendor.
  - `mask_tax_id("12-3456789") == "•••-••-6789"`.
  - An income account as the default expense account is rejected.
- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: vendors`

---

### Task 2: Purchasing models, numbering, balances, hooks

**Files:**
- Create: `purchasing/__init__.py`, `apps.py`, `models.py`, `errors.py`, `balances.py`, `admin.py`, migrations (one seeds `Threshold1099` 2025 = 600.00 and 2026 = 2000.00)
- Modify: `sales/models.py` and migration (DocType values, `max_length=10`), `sales/numbering.py`, `dnb/settings.py`, `tests/conftest.py`
- Create: `sales/hooks.py`
- Test: `tests/test_purchasing_models.py`

**Interfaces:**
- Produces:
  - `sales.DocType` gains `PO`, `BILL`, `VP`, `VC`.
  - `sales.numbering.register_document(doc_type, model, prefix: str, padding: int) -> None` fills `DEFAULTS` and `MODELS`. `purchasing.apps.ready()` registers PO (`PO-`, 5), BILL (`BILL-`, 5), VP (`VP-`, 5), VC (`VC-`, 5).
  - `sales.hooks.register_invoice_changed(fn: Callable[[Invoice], None])` and `invoice_changed(invoice) -> None`.
  - `purchasing/models.py`: all spec section 4 models. Choices:
    - `BillLineKind` (`STOCK`, `DROP_SHIP`, `NON_STOCK`)
    - `VendorPaymentMethod` (`CHECK`, `ACH`, `CARD`)
    - `VendorCreditKind` (`RETURN`, `ALLOWANCE`)
    - `DocStatus` and `OrderStatus` reused from `sales.models`
  - `PurchasingSettings.load()`, `is_configured`, `require_configured()`.
  - `DropShipAllocation`: FKs to `BillItemLine` and `sales.InvoiceLine`, both `on_delete=CASCADE`.
- Produces (`purchasing/balances.py`, same signatures and as-of rules as `sales.balances`, reusing its `_counts` and `_active`):
  - `bill_total(bill)`, `bill_applied(bill, as_of=None)`, `bill_balance(bill, as_of=None)`
  - `vendor_payment_unapplied(payment, as_of=None)`
  - `credit_total(credit)`, `vendor_credit_unapplied(credit, as_of=None)`
  - `vendor_balance(vendor, as_of=None)`
  - Bulk loaders `bills_with_balances()`, `vendor_payments_with_balances()`, `vendor_credits_with_balances()`.
- Fixtures: `purchasing_settings` (A/P = existing `ap` 2000), `card` (2100 CREDIT_CARD "Company Visa"), `freight` (6150 EXPENSE "Freight in").

- [x] **Step 1: Failing tests:**
  - `next_number(DocType.BILL) == "BILL-00001"` inside a transaction.
  - `set_sequence` works for VP.
  - The seeded thresholds exist.
  - Balances as of a date before and after a removed application.
- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run full suite, expect PASS** (Phase 3 numbering tests unchanged)
- [x] **Step 5: Commit** `feat: purchasing models, numbering, balances`

---

### Task 3: Purchase orders and drop-ship POs (service)

**Files:**
- Create: `purchasing/posting/__init__.py`, `purchasing/posting/orders.py`
- Test: `tests/test_purchase_orders.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) POLineSpec(item: Item, quantity: int, price: Decimal, description: str = "", so_line: SalesOrderLine | None = None, line_id: int | None = None)`.
  - `create_po(*, vendor, date, lines, user, expected_date=None, notes="", sales_order=None) -> PurchaseOrder`.
  - `update_po(po, *, lines, user, ...)`: same rules as sales orders. "Line {n}: {billed} already billed." "Line {n} has been billed and can't be removed." "{number} is closed. Reopen it to make changes."
  - `billed_quantity(line, exclude_bill=None) -> int`, `po_remaining(line, exclude_bill=None) -> int`.
  - `refresh_po_status(po)`, `close_po`, `reopen_po`.
  - `drop_ship_candidates(order: SalesOrder) -> dict[str, list[SalesOrderLine]]`: drop-ship lines not yet on a non-void PO, grouped by the vendor text as typed.
  - `create_drop_ship_pos(order, *, user, date, vendor_choices: dict[str, Vendor] | None = None) -> list[PurchaseOrder]`:
    - One PO per vendor. Each vendor name is resolved with `Vendor.find_by_name`, unless it's in `vendor_choices`.
    - Any unresolved name raises `PurchasingError("No vendor named {name}. Choose a vendor for these lines.")` with attribute `unresolved: list[str]`, and nothing is created.
    - Line quantity = SO line quantity. Price = the item's `purchase_cost`.

- [x] **Step 1: Failing tests:**
  - PO create, update, and close rules.
  - Drop-ship POs: two vendors make two POs.
  - Lines already on a PO are skipped.
  - Fuzzy name match works.
  - An unmatched name raises and creates nothing (Review Focus 2).
  - With `vendor_choices`, it succeeds.
- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: purchase orders and drop-ship POs`

---

### Task 4: Bills and drop-ship allocation (service)

**Files:**
- Create: `purchasing/posting/bills.py`, `purchasing/dropship.py`
- Modify: `sales/posting/invoices.py` (call `sales.hooks.invoice_changed(invoice)` at the end of `create_invoice`, `edit_invoice`, `void_invoice`), `purchasing/apps.py` (register the hook)
- Test: `tests/test_bills.py`, `tests/test_dropship.py`

**Interfaces:**
- Produces (`bills.py`):
  - `@dataclass(frozen=True) BillItemSpec(item: Item, quantity: int, price: Decimal, description: str = "", po_line: PurchaseOrderLine | None = None)`. The kind is derived:
    - DROP_SHIP when `po_line.so_line` is set.
    - Otherwise STOCK if `item.is_stock`.
    - Otherwise NON_STOCK.
  - `@dataclass(frozen=True) BillExpenseSpec(account: Account, amount: Decimal, description: str = "")`.
  - `DuplicateBillWarning(PurchasingError)`, with the spec 5.4 message.
  - `create_bill(*, vendor, date, vendor_invoice_number, item_lines=(), expense_lines=(), user, purchase_order=None, terms=None, notes="", allow_duplicate=False, allow_negative=False) -> Bill`.
  - `bill_from_po(po, *, date, vendor_invoice_number, quantities: dict[int, int], prices: dict[int, Decimal] | None = None, expense_lines=(), user, allow_duplicate=False) -> Bill`.
- Posting (spec 5.1):
  1. Validate everything first.
  2. Stock lines: one `record_movements(kind=RECEIPT, lines=[StockLine(item, +q, unit_cost=price, account=ap_account)], source_type="purchasing.bill", source_id=number)` call for all stock lines.
  3. One `purchasing.bill` entry for all other lines, Cr A/P. The debit accounts, grouped:
     - DROP_SHIP: `item.cogs_account`, or `InventorySettings.default_cogs_account` for non-stock items.
     - NON_STOCK: `vendor.default_expense_account`, else `InventorySettings.default_cogs_account`.
     - Expense: its account.
     - If a needed default is missing: "Line {n}: choose an account for {sku}."
  4. Then `dropship.rebuild(so_line_ids)` for every DROP_SHIP line.
- Validation and messages:
  - "A bill needs at least one line."
  - "Line {n}: only {left} left to receive on {po_number}."
  - "{po_number} is for a different vendor."
  - The vendor must be active: "{name} is inactive."
  - Stock lines with inventory settings incomplete raise the Phase 2 `SettingsMissingError` (Review Focus 3).
  - Duplicate check: `vendor_invoice_number` normalized with `strip().casefold()` and inner whitespace collapsed, compared against non-void bills of the same vendor (Review Focus 4).
- Produces (`dropship.py`):
  - `rebuild(so_line_ids: Iterable[int]) -> None`. For each SO line:
    1. Delete its allocations.
    2. Take the billed DROP_SHIP quantities from non-void bills, oldest bill date first.
    3. Allocate them to non-void invoice lines for that SO line, oldest invoice date first.
    4. Set each invoice line's `drop_ship_billed = allocated == quantity`.
  - `on_invoice_changed(invoice) -> None` calls `rebuild` for the invoice's drop-ship SO lines, including the lines a just-edited invoice used to have (taken from its latest `InvoiceRevision` snapshot).

- [ ] **Step 1: Failing tests**

```python
def test_stock_bill_raises_stock_at_bill_price(...):    # PO 1000 @0.50; bill 500 @0.48 → on_hand +500, layer 0.48; A/P 240.00
def test_mixed_bill_posts_expense_line(...):            # + freight 25.00 → Dr 6150 25.00; A/P total 265.00
def test_partial_receiving_and_auto_close(...):
def test_over_receiving_refused(...):                   # "Line 1: only 500 left to receive on PO-00001."
def test_duplicate_vendor_invoice_number_warns(...):    # "inv-88123 " vs "INV-88123"; allow_duplicate=True saves
def test_stock_line_needs_inventory_settings(...):      # Review Focus 3
def test_expense_only_bill_without_inventory_settings(...):
def test_drop_ship_bill_posts_cogs_no_stock(...):       # Dr item COGS, Cr A/P; on_hand unchanged
def test_drop_ship_allocation_oldest_first(...):        # SO line 3; invoices of 1 and 2; bill 2 → first invoice billed, second not
def test_bill_before_invoice_allocates_on_invoice(...): # bill first, then invoice → invoice line drop_ship_billed True
def test_invoice_void_releases_allocation(...):
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review against the `finance:journal-entry` skill and record findings in the commit body.
- [ ] **Step 4: Run full suite, expect PASS** (Phase 3 tests unchanged)
- [ ] **Step 5: Commit** `feat: bills with stock, drop-ship, and expense lines`

---

### Task 5: Edit and void bills (service)

**Files:**
- Modify: `purchasing/posting/bills.py`
- Test: `tests/test_bill_edit_void.py`

**Interfaces:**
- Produces:
  - `edit_bill(bill, *, user, date, vendor_invoice_number, item_lines, expense_lines, terms=None, notes="", vendor=None, reason="", allow_duplicate=False, allow_negative=False) -> Bill`. It follows Phase 3 `edit_invoice`:
    - The snapshot JSON has `vendor_id`, `vendor_invoice_number`, `date`, `terms`, `due_date`, `notes`, `item_lines`, and `expense_lines`.
    - It system-voids the entry and the receipt movements dated the old bill date. The movement voids pass `allow_negative`, so a `NegativeStockWarning` propagates for stock already sold.
    - It reposts, then runs `dropship.rebuild`.
    - PO remaining is calculated with `exclude_bill=bill`.
  - `void_bill(bill, *, user, reason, date=None, allow_negative=False) -> Bill`: blocked by active applications (spec message). Then the stock voids, `refresh_po_status`, and `dropship.rebuild`.

- [ ] **Step 1: Failing tests:**
  - Snapshot and reposting.
  - Editing the price of a stock line recosts a later sale (an invoice made after the bill shows the changed COGS through a Phase 2 revision).
  - Voiding a bill whose stock was sold raises `NegativeStockWarning`, then succeeds with `allow_negative`.
  - An edit below the applied amount is refused.
  - A void with applications is refused.
  - A Standard user can edit but not void.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: edit and void bills`

---

### Task 6: Vendor payments and Pay bills (service)

**Files:**
- Create: `purchasing/posting/payments.py`
- Test: `tests/test_vendor_payments.py`

**Interfaces:**
- Produces:
  - `pay_vendor(*, vendor, date, method: VendorPaymentMethod, amount, paid_from: Account, user, check_number: str = "", reference: str = "", applications: Sequence[tuple[Bill, Decimal]] = ()) -> VendorPayment`. The entry is Dr A/P, Cr `paid_from`.
  - `@dataclass(frozen=True) PayLine(bill: Bill, amount: Decimal)`.
  - `pay_bills(*, date, method, paid_from, lines: Sequence[PayLine], user, check_numbers: dict[int, str] | None = None) -> list[VendorPayment]`. It makes one payment per vendor, ordered by vendor name, inside one transaction. `check_numbers` is keyed by vendor id.
  - `apply_vendor_payment(payment, bill, amount, *, date, user)`, `remove_bill_application(app, *, date, user)`.
  - `void_vendor_payment(payment, *, user, reason, date=None) -> VendorPayment`. It removes active applications on the void date.
- Rules and messages:
  - Methods: CHECK and ACH need a BANK account ("Choose the bank account to pay from."); CARD needs a CREDIT_CARD account ("Choose the credit card used."). CHECK needs a number: "Enter the check number for {vendor}."
  - Check numbers must be unique per `paid_from` among non-void CHECK payments, including others in the same batch (Review Focus 1).
  - "{bill} has {balance} left to pay."
  - "This payment has {unapplied} left to apply."
  - "The date can't be before {number}'s date." (Review Focus 5)
  - "{bill} belongs to a different vendor."

- [ ] **Step 1: Failing tests:**
  - A batch of three bills from two vendors makes two payments with the correct applications.
  - A partial payment.
  - A card payment credits the card account.
  - A duplicate check number, within a batch and across batches, is refused and nothing is saved.
  - A payment dated before a bill is refused.
  - A single payment over the bills keeps the extra as unapplied, and it's applied later.
  - A void reopens the bills.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: vendor payments and pay bills`

---

### Task 7: Vendor credits (service)

**Files:**
- Create: `purchasing/posting/credits.py`
- Test: `tests/test_vendor_credits.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ReturnLineSpec(item: Item, quantity: int, amount: Decimal | None = None)`. A `None` amount means quantity × `current_unit_cost(item)`, rounded.
  - `@dataclass(frozen=True) AllowanceLineSpec(account: Account, amount: Decimal, description: str = "")`.
  - `create_return_credit(*, vendor, date, lines, reason, user, allow_negative=False) -> VendorCredit`, per spec 5.1. Stock goes out through `record_movements(kind=RECEIPT, quantity=-q, account=item.cogs_account)`, then one entry: Dr A/P (Σ amounts), Cr each item's COGS account (its amount).
  - `create_allowance_credit(*, vendor, date, lines, reason, user) -> VendorCredit`. The account defaults to the vendor's default expense account; with none set: "Line {n}: choose an account."
  - `apply_vendor_credit(credit, bill, amount, *, date, user)`.
  - `void_vendor_credit(credit, *, user, reason, date=None, allow_negative=False)`: blocked by applications (spec message). A return void puts the stock back through a system void of its movements.

- [ ] **Step 1: Failing tests:**
  - A return of 10 FIFO @ 0.48 credited at 5.00: inventory −4.80, A/P −5.00, COGS net −0.20.
  - The negative-stock warning on a return.
  - An allowance posts to the vendor's default account.
  - Apply to a bill.
  - A void restores stock.
  - `check_invariants() == []` after each step.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: vendor credits and returns`

---

### Task 8: A/P check and close

**Files:**
- Create: `purchasing/checks.py`
- Modify: `purchasing/apps.py`
- Test: `tests/test_ap_check.py`

**Interfaces:**
- Produces:
  - `ap_check(as_of: date | None = None) -> list[str]`. A/P ledger balance = credits − debits on the settings A/P account. The message is "Vendor balances say {x}, the A/P account says {y}."
  - `close_check(year, month) -> list[str]`, prefixed "{Month YYYY}: " and registered in `ready()`.

- [ ] **Step 1: Failing tests:**
  - Empty after a mixed scenario.
  - A manual entry to A/P is reported and blocks the close; the admin override works.
  - A `hypothesis.extra.django.TestCase` property test: random sequences of PO, bill (stock, drop-ship, expense), edit, payment, application, removal, credit, and void keep `ap_check`, `items.stock.check_invariants`, `sales.checks.ar_check`, and `undeposited_check` empty, at the end and at every date touched.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review against the `finance:reconciliation` skill and record findings in the commit body.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: A/P check at month close`

---

### Task 9: Purchasing reports and 1099 summary

**Files:**
- Create: `purchasing/reports.py`, `purchasing/csv_export.py`, `purchasing/views/reports.py`, templates
- Modify: `ledger/urls.py`, `core/templates/core/home.html` (report links)
- Test: `tests/test_purchasing_reports.py`

**Interfaces:**
- Produces:
  - `ap_aging(as_of)`, with the same shape as Phase 3's `ar_aging` (vendor rows, buckets, unapplied, ledger_balance, difference).
  - `bills_due(through: date)`, `vendor_balances(as_of=None)`, `open_purchase_orders()`.
  - `summary_1099(year: int) -> Summary1099(year, threshold: Decimal | None, rows: list[Row1099(vendor, total, file: bool)])`. It sums non-void CHECK and ACH payments dated in the year for `is_1099` vendors.
  - URL names `reports:ap-aging`, `reports:bills-due`, `reports:vendor-balances`, `reports:open-purchase-orders`, `reports:1099-summary`, all with CSV.
  - The 1099 CSV:
    - Admin only (403 for Standard).
    - Full tax ID.
    - Columns `vendor,tax_id,address,total_paid,file`.
  - The page shows the spec note text and the missing-threshold message.

- [ ] **Step 1: Failing tests:**
  - Aging buckets and the ledger tie.
  - 1099: card excluded; a payment on Dec 31 vs Jan 1 lands in the right year; the 2026 threshold of 2000.00 marks 1,999.99 as not file and 2,000.00 as file; a missing year shows the message.
  - A Standard user sees masked tax IDs, and the CSV returns 403.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the five reports (light and dark), sent.
- [ ] **Step 6: Commit** `feat: purchasing reports and 1099 summary`

---

### Task 10: Vendor and purchasing settings screens

**Files:**
- Create: `purchasing/forms.py` (settings, thresholds), `purchasing/views/vendors.py`, `purchasing/views/settings.py`, templates, `purchasing/urls.py`, `purchasing/demo.py`
- Modify: `dnb/urls.py` (`vendors/`, `purchasing/`), `core/templates/core/base.html` (Purchasing sidebar group; Setup gains Purchasing settings), `core/management/commands/seed_demo.py`
- Test: `tests/test_vendor_views.py`

**Interfaces:**
- Produces the spec section 7 vendor and settings screens.
- The vendor detail page shows open bills, unapplied credits and payments with "Apply", and history.
- Deactivating a vendor with a balance: spec message.
- Settings (admin): the A/P account (ACCOUNTS_PAYABLE subtype), the numbering table (Phase 3 `set_sequence` for PO, BILL, VP, VC), and 1099 thresholds (add or edit a year; amount > 0).

- [ ] **Step 1: Failing tests:**
  - Vendor CRUD.
  - Masking for Standard users on list and detail.
  - The deactivate rule.
  - Settings admin only.
  - Threshold editing.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (demo: 4 vendors, one 1099 vendor, settings)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the vendor list, vendor detail, vendor form, and purchasing settings (light and dark), sent.
- [ ] **Step 6: Commit** `feat: vendor and purchasing settings screens`

---

### Task 11: Purchase order and bill screens

**Files:**
- Create: `purchasing/views/orders.py`, `purchasing/views/bills.py`, templates
- Modify: `purchasing/forms.py`, `purchasing/urls.py`, `purchasing/demo.py`, `sales/templates/sales/order_detail.html` ("Create drop-ship PO" button shown when `drop_ship_candidates` is non-empty), `sales/urls.py` or `purchasing/urls.py` (`purchasing:drop-ship-po` `/purchasing/orders/drop-ship/<so_number>/`)
- Test: `tests/test_po_bill_views.py`

**Interfaces:**
- Produces the spec section 7 PO and bill screens.
- Forms reuse `sales/views/common.py` row parsing and the one-time token. The bill form has two line tables (item lines, expense lines; up to 30 each).
- A duplicate vendor invoice number and negative stock each re-render a confirm page ("Save anyway").
- The drop-ship PO page lists the vendor names. Unresolved ones get a vendor select (Review Focus 2) before "Create POs".
- Bill detail shows drop-ship allocations linking to invoices.

- [ ] **Step 1: Failing tests:**
  - Create a PO and bill it partially through the views.
  - Over-receiving shows the message.
  - The duplicate confirm flow.
  - A drop-ship PO with an unmatched vendor, then choosing one.
  - A double submit posts once.
  - Edit creates a revision; void is admin only.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (demo: 2 POs, 3 bills including a drop-ship and an expense bill)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the PO list, PO form, PO detail, the drop-ship PO page (with an unmatched vendor), the sales order with the new button, bill form (from a PO, with an expense line), duplicate confirm, bill detail with allocations, and a bill revision. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: purchase order and bill screens`

---

### Task 12: Pay bills, vendor payment, and vendor credit screens

**Files:**
- Create: `purchasing/views/payments.py`, `purchasing/views/credits.py`, templates, `purchasing/static/purchasing/pay.js` (small vanilla JavaScript for running totals)
- Modify: `purchasing/forms.py`, `purchasing/urls.py`, `purchasing/demo.py`
- Test: `tests/test_payment_credit_views.py`

**Interfaces:**
- **Pay bills:** filters (vendor, due through), one row per open bill with a pay amount, a "Pay in full" button, method, pay-from account (filtered by method), and a check number field per vendor group when the method is CHECK. The result page lists the payments created.
- **Single payment, credit (return or allowance), apply, void** per spec section 7.

- [ ] **Step 1: Failing tests:**
  - Pay bills creates one payment per vendor.
  - The duplicate check number message.
  - Card requires a card account.
  - A return credit through the view moves stock.
  - Applying a credit.
  - Voids are admin only.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (demo: payments by check, ACH, and card; one return credit)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of Pay bills (with a check-number error), the payments created, payment detail, the return credit form, the allowance credit form, and credit detail. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: pay bills, vendor payment, and vendor credit screens`

---

### Task 13: Home, acceptance, docs, PR

**Files:**
- Modify: `core/views.py`, `core/templates/core/home.html`, `README.md` (Status; What it does: Purchasing row)
- Create: `tests/test_phase4_acceptance.py`

- [ ] **Step 1: Home cards:** "Bills due in 7 days" (total, linking to Bills due) and "Overdue bills" (total). Test the values.
- [ ] **Step 2: Acceptance test** (spec section 1, through the test client as an admin, with `ap_check`, `ar_check`, `undeposited_check`, and `items.stock.check_invariants` all empty after each step), plus the 1099 summary totals at the end.
- [ ] **Step 3: Full suite** `uv run pytest -q` passes with no new warnings.
- [ ] **Step 4: Final screenshots:** a tour (home, vendor, PO, bill, Pay bills, A/P aging, 1099 summary), light and dark, sent.
- [ ] **Step 5: Commit** `test: phase 4 acceptance`
- [ ] **Step 6: Open the PR** `Phase 4: Purchasing`. **The body must be filled in** per `CLAUDE.md`: a task table, the test output, deviations, screenshot sets, and manual checks:
  - Run `./update.sh`.
  - Fill in Purchasing settings.
  - Set next numbers to continue Zoho.
  - Mark 1099 vendors and enter tax IDs.
  - Create the company credit card account in the chart of accounts.

# Phase 3: Sales Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers, sales orders, invoices per shipment (with drop-ship lines and editable saved invoices), payments, deposits, credit notes, refunds, AR and Undeposited Funds checks with month-close override, sales reports, and WeasyPrint invoice PDFs, all on the Ledger paper screens.

**Architecture:**
- **New apps:** `contacts` (Customer) and `sales` (documents, settings, numbering, reports, PDF).
- **One way in:** all sales postings go through the `sales/posting/` package (`orders.py`, `invoices.py`, `receipts.py`, `deposits.py`). It calls `ledger.posting` for money and `items.stock` for stock. Views never post.
- **Balances are computed, not stored:** they come from documents plus dated application rows, so any past date can be rebuilt.

**Tech Stack:** Phase 1 and 2 stack, plus **WeasyPrint** (`weasyprint>=70,<71`, verified to install from PyPI and render in cloud sessions with the system's pango). No other new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-27-phase-3-sales-design.md`. Visual references: `docs/design/items-mockup.html` (screens), `docs/design/invoice-mockup.html` (PDF).

## Global Constraints

- **`CLAUDE.md` applies in full,** including the standing screenshot rule.
- **Money:**
  - `Decimal` throughout.
  - Prices: `DecimalField(14, 4)`. Amounts and totals: `DecimalField(14, 2)`.
  - A line amount is `round_half_up(quantity × price, 0.01)`.
  - Quantities are whole numbers.
- **Journal `source_type` values:**
  - `sales.invoice`, `sales.payment`, `sales.deposit`, `sales.credit_note`, `sales.refund`.
  - Source id is the document number.
  - Stock movements for invoices use `MovementKind.SALE`, movement `source_type="sales.invoice"`, `source_id=<invoice number>`.
- **Zero totals:** a document whose total is 0.00 posts no journal entry, and its `journal_entry` is null.
- **Voids:**
  - Admin only. A reason is required.
  - The default date is the document date. It must be in an open month.
  - Voided documents keep their numbers.
- **Settings missing:** with sales settings incomplete, every posting raises `SalesSettingsMissing("Set up sales accounts first.")`. The screens show it with a link to `/sales/settings/`.
- **Exact strings:** user-facing messages are exactly the strings in the spec or this plan.
- **Spec clarification (module name):** spec section 3 says `sales/posting.py`. It is a package, `sales/posting/`, split by document family to keep files focused. The rule it carries (the only way sales posts) is unchanged.
- **Spec clarification (voiding a payment):** voiding a payment that has active applications removes those applications on the void date, so the invoices reopen (this is how a bounced check is undone). The spec 5.4 blocks (deposit, refund) still apply first.

## Review Focus

1. **Two people saving invoices at the same moment** → different numbers, no gap from a failed save. Pinned in Task 3 (threaded test).
2. **Editing an invoice that came from a sales order** → its own current quantities count as available. Raising the quantity up to (remaining + this invoice's current quantity) is allowed; above that is refused with the 5.2 message. Pinned in Task 6.
3. **An item made inactive after it was put on an order** → invoicing that line fails with "Line {n}: {sku} is not an active stock item." (the Phase 2 message) and nothing is saved, not a 500. Pinned in Task 5.
4. **Inventory settings not configured** → an invoice with only drop-ship or non-stock lines still saves. One stock line raises "Set up inventory accounts first." Pinned in Task 5.
5. **Logo upload that isn't a PNG or JPEG, or is over 1 MB** → "Logo must be a PNG or JPEG under 1 MB.", and the old logo is kept. Pinned in Task 12.

---

## File Structure

```
ledger/models.py, migrations/00xx    + AccountingPeriod.close_override_reason, close_override_problems
ledger/periods.py                    close_period(..., override_reason="")
ledger/posting.py                    void_entry(..., system=False)
ledger/admin.py, templates           blocked close → "Close anyway" view
items/stock.py                       void_movement(..., system=False)
contacts/  models.py (Customer, Terms, due_date), forms.py, views.py, urls.py, templates/contacts/*
sales/
  models.py           SalesSettings, DocumentSequence, SalesOrder(+Line), Invoice(+Line, Revision), Payment,
                      PaymentApplication, CreditNote(+Line), CreditApplication, Deposit, Refund
  numbering.py        next_number, set_sequence
  errors.py           SalesError hierarchy
  balances.py         totals, balances, unapplied amounts, as-of variants
  posting/__init__.py, orders.py, invoices.py, receipts.py, deposits.py
  checks.py           ar_check, undeposited_check, close checks registration (apps.ready)
  reports.py, csv_export.py
  pdf.py, templates/sales/invoice_pdf.html
  forms.py, views/ (orders.py, invoices.py, receipts.py, deposits.py, settings.py, reports.py), urls.py
  templates/sales/*
  demo.py             seed_demo extension
Dockerfile            + libharfbuzz-subset0
tests/  test_close_override.py, test_customers.py, test_sales_models.py, test_numbering.py, test_orders.py,
        test_invoicing.py, test_invoice_edit_void.py, test_receipts.py, test_deposits.py, test_sales_checks.py,
        test_sales_reports.py, test_invoice_pdf.py, test_customer_views.py, test_order_invoice_views.py,
        test_receipt_views.py, test_phase3_acceptance.py
```

---

### Task 1: Month-close override and system voids

**Files:**
- Modify: `ledger/models.py`, new migration, `ledger/periods.py`, `ledger/posting.py`, `ledger/admin.py`, `items/stock.py`
- Create: `ledger/templates/admin/ledger/accountingperiod/close.html`
- Test: `tests/test_close_override.py`

**Interfaces:**
- Produces:
  - `AccountingPeriod.close_override_reason: TextField(blank=True)`, `close_override_problems: TextField(blank=True)`.
  - `close_period(year, month, user, override_reason: str = "") -> AccountingPeriod`:
    - Problems and a blank reason raise `PeriodCloseBlockedError(" ".join(problems))`, as today.
    - Problems and a non-blank reason (admin, already enforced) close the month and store the reason and the joined problems.
    - No problems: the reason is ignored and not stored.
  - Admin: the "Close selected periods" action, for each blocked period, shows the problems plus a link "Close anyway" to `admin:ledger_accountingperiod_close` (`<id>/close/`). That page shows the problems and a required `reason` field ("Enter a reason to close anyway.").
  - `ledger.posting.void_entry(..., system: bool = False)`: `system=True` skips `require_admin` (still requires a reason). `items.stock.void_movement(..., system: bool = False)`, same. Neither flag is passed by any view.

- [x] **Step 1: Write failing tests**

```python
def test_override_closes_and_records(admin_user, blocked_check):   # fixture registers a check returning ["Off by 1.00."]
    p = close_period(2026, 10, admin_user, override_reason="Known timing difference")
    assert p.is_closed and p.close_override_reason == "Known timing difference"
    assert p.close_override_problems == "Off by 1.00."

def test_blank_reason_still_blocks(admin_user, blocked_check):
    with pytest.raises(PeriodCloseBlockedError, match="Off by 1.00."):
        close_period(2026, 10, admin_user, override_reason="  ")

def test_standard_user_cannot_override(standard_user, blocked_check): ...   # PermissionDenied
def test_admin_close_anyway_page(client, admin_user, blocked_check): ...     # GET shows problem; POST without reason re-shows form; with reason closes
def test_system_void_entry_skips_admin(standard_user, posted_entry): ...     # void_entry(..., system=True) works for Standard
def test_system_void_movement_skips_admin(...): ...
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run full suite, expect PASS**
- [x] **Step 5: Screenshots** of the blocked close message and the "Close anyway" page (light and dark), sent.
- [x] **Step 6: Commit** `feat: admin can close a month anyway with a recorded reason`

---

### Task 2: Customers (model)

**Files:**
- Create: `contacts/__init__.py`, `apps.py`, `models.py`, `admin.py`, migrations
- Modify: `dnb/settings.py`, `core/management/commands/setup_roles.py`, `tests/conftest.py`
- Test: `tests/test_customers.py`

**Interfaces:**
- Produces:
  - `Terms(TextChoices)`: `DUE_ON_RECEIPT` "Due on receipt", `NET_15` "Net 15", `NET_30` "Net 30", `NET_45` "Net 45", `NET_60` "Net 60".
  - `TERM_DAYS = {DUE_ON_RECEIPT: 0, NET_15: 15, NET_30: 30, NET_45: 45, NET_60: 60}`.
  - `due_date(terms: Terms, on: date) -> date`.
  - `Customer` with the spec fields. `name` is required and unique case-insensitively ("A customer named {name} already exists."). The default terms are NET_30. Registered with auditlog.
- `setup_roles`: Standard gets add, change, and view on `contacts.customer`. Admin gets all permissions for `contacts` and `sales` (add both to `ADMIN_APPS`).
- `conftest.py`: a `customer` fixture ("Acme Builders", NET_30, a billing and a ship-to address).

- [ ] **Step 1: Failing tests:** `due_date(NET_30, date(2026, 10, 27)) == date(2026, 11, 26)`; due on receipt is the same day; a duplicate name (different case) is rejected with the message.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: customers with payment terms`

---

### Task 3: Sales models, settings, numbering, balances

**Files:**
- Create: `sales/__init__.py`, `apps.py`, `models.py`, `errors.py`, `numbering.py`, `balances.py`, `admin.py`, migrations (one data migration seeds sequences)
- Modify: `dnb/settings.py` (`sales`; `MEDIA_ROOT = DNB_DB_PATH.parent / "media"`, `MEDIA_URL = "/media/"`, not publicly served), `tests/conftest.py`
- Test: `tests/test_sales_models.py`, `tests/test_numbering.py`

**Interfaces:**
- Produces (`sales/models.py`): every model and field in spec section 4, plus:
  - `Fulfillment(TextChoices)`: `STOCK` "Ship from stock", `DROP_SHIP` "Drop ship".
  - `PaymentMethod`: `CHECK`, `CASH`, `ACH`, `WIRE`.
  - `DocStatus`: `OPEN`, `VOID` (SalesOrder: `OPEN`, `CLOSED`).
  - `DocType`: `SO`, `INV`, `PMT`, `CN`, `DEP`, `REF`.
  - `SalesSettings.load()`, `is_configured` (ar, undeposited, returns accounts set).
  - `Refund` has two nullable FKs, `source_payment` and `source_credit_note`, with a DB check constraint requiring exactly one.
  - Applications carry `applied_on: date`, `removed: bool`, `removed_on: date | None`.
- Seeded sequences (data migration):

  | DocType | Prefix | Padding | Next number |
  |---|---|---|---|
  | SO | `SO-` | 5 | 1 |
  | INV | `INV-` | 6 | 1 |
  | PMT | `PMT-` | 5 | 1 |
  | CN | `CN-` | 5 | 1 |
  | DEP | `DEP-` | 5 | 1 |
  | REF | `REF-` | 5 | 1 |

- Produces (`sales/numbering.py`):
  - `next_number(doc_type: DocType) -> str`. It must be called inside the caller's `transaction.atomic()`. It reads and increments the sequence, so a rolled-back save gives the number back.
  - `set_sequence(doc_type, *, prefix: str, next_number: int, user) -> DocumentSequence`. Admin only. It refuses when the number it would produce exists on that document type: "{number} is already used. Choose a higher next number."
- Produces (`sales/errors.py`): `SalesError(Exception)` with subclasses `SalesSettingsMissing`, `OrderError`, `InvoiceError`, `ReceiptError`, `DepositError`.
- Produces (`sales/balances.py`), all `Decimal` at 2 dp, `as_of: date | None` meaning "now":
  - `invoice_total(invoice) -> Decimal`.
  - `invoice_balance(invoice, as_of=None) -> Decimal`. An application counts if `applied_on <= as_of` and not `removed_on <= as_of`.
  - `payment_unapplied(payment, as_of=None)`, `credit_unapplied(credit_note, as_of=None)`. Each subtracts active applications and non-void refunds dated `<= as_of`.
  - `customer_balance(customer, as_of=None)`.
  - An invoice counts as of a date when `date <= as_of` and it is not voided on or before `as_of`. Payments and credit notes, the same.

- [ ] **Step 1: Failing tests**

```python
def test_numbers_are_sequential_and_padded(db):
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000001"
        assert next_number(DocType.INV) == "INV-000002"

def test_rolled_back_save_returns_the_number(db): ...
@pytest.mark.django_db(transaction=True)
def test_concurrent_numbers_are_unique(ledger_purge): ...      # Review Focus 1: 2 threads × 20 numbers, 40 unique
def test_set_sequence_refuses_used_number(admin_user, customer): ...   # Invoice row created directly as INV-000005; next 5 refused, 6 ok
def test_balances_as_of_respect_removed_applications(...): ...  # applied Oct 5, removed Oct 9: balance Oct 6 vs Oct 10
def test_refund_needs_exactly_one_source(db): ...                # IntegrityError
```

`conftest.py` gains `sales_settings` (A/R 1200, Undeposited Funds 1250 OTHER_CURRENT_ASSET, Sales returns 4900 INCOME) and a `bank` (1000 BANK) fixture.

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: sales models, numbering, and balances`

---

### Task 4: Sales orders (service)

**Files:**
- Create: `sales/posting/__init__.py`, `sales/posting/orders.py`
- Test: `tests/test_orders.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) OrderLineSpec(item: Item, quantity: int, price: Decimal, description: str = "", fulfillment: Fulfillment = STOCK, drop_ship_vendor: str = "", line_id: int | None = None)`.
  - `create_order(*, customer, date, lines: Sequence[OrderLineSpec], user, ship_to: str | None = None, notes: str = "") -> SalesOrder`. `ship_to` defaults to the customer's.
  - `update_order(order, *, lines, user, date=None, ship_to=None, notes=None) -> SalesOrder`. Lines carrying `line_id` update that line, and omitted lines are deleted.
  - `invoiced_quantity(line: SalesOrderLine, exclude_invoice: Invoice | None = None) -> int`: sum over non-void invoices.
  - `remaining(line, exclude_invoice=None) -> int`.
  - `close_order(order, user) -> SalesOrder`, `reopen_order(order, user) -> SalesOrder`, `refresh_status(order) -> None`. `refresh_status` closes the order when every line's remaining is 0; it is called by invoicing.
- Rules and messages:
  - Quantity must be > 0: "Line {n}: enter a quantity above zero."
  - A drop-ship line needs a vendor: "Line {n}: enter the drop-ship vendor."
  - Lowering a line below what's invoiced: "Line {n}: {invoiced} already invoiced."
  - Deleting an invoiced line: "Line {n} has been invoiced and can't be removed."
  - Editing a closed order: "{number} is closed. Reopen it to make changes."
  - No posting.

- [ ] **Step 1: Failing tests:** create; update rules and messages; auto-close and manual close or reopen; `remaining` ignores void invoices.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: sales orders`

---

### Task 5: Invoicing (service)

**Files:**
- Create: `sales/posting/invoices.py`
- Test: `tests/test_invoicing.py`

**Interfaces:**
- Consumes: `next_number`, `due_date`, `remaining`, `refresh_status`, `ledger.posting.post_entry`, `items.stock.record_movements` (`StockLine(item, -qty, account=item.cogs_account)`).
- Produces:
  - `@dataclass(frozen=True) InvoiceLineSpec(item: Item, quantity: int, price: Decimal, description: str = "", fulfillment: Fulfillment = STOCK, drop_ship_vendor: str = "", so_line: SalesOrderLine | None = None)`.
  - `create_invoice(*, customer, date, lines, user, sales_order=None, terms=None, ship_to=None, notes=None, allow_negative=False) -> Invoice`. Defaults: terms from the customer, `ship_to` from the order or customer, notes from `SalesSettings.default_invoice_notes`.
  - `invoice_from_order(order, *, date, quantities: dict[int, int], user, allow_negative=False, ship_to=None, notes=None) -> Invoice`. Keys are SO line ids. Lines with 0 are skipped. Line price, description, fulfillment, and vendor come from the SO line.
- Posting:
  1. Validate everything first, so nothing posts on error.
  2. One journal entry: Dr A/R (total), Cr income accounts grouped (each line's `item.income_account`), memo `"Invoice {number} {customer}"`.
  3. Stock lines: one `record_movements(kind=SALE, ...)` call for all stock lines, `source_id=number`. `NegativeStockWarning` propagates (nothing saved) unless `allow_negative`. Each `InvoiceLine.stock_movement` is linked.
  4. Drop-ship and non-stock-item lines: no movement. A non-stock item with STOCK fulfillment is allowed and moves nothing.
  5. Then `refresh_status(order)`.
- Rules and messages:
  - "Line {n}: only {left} left to invoice on {so_number}."
  - "An invoice needs at least one line with a quantity."
  - "Line {n}: price can't be negative."
  - The customer must be active: "{name} is inactive."
  - Invoice date in a closed month: Phase 1 `PeriodClosedError`.

- [ ] **Step 1: Failing tests**

```python
def test_invoice_posts_revenue_and_cogs(...):
    # FIFO item 10 @ 0.50 on hand; invoice 6 @ 0.85 → Dr A/R 5.10 / Cr Sales 5.10; Dr COGS 3.00 / Cr Inventory 3.00
def test_partial_shipments_and_auto_close(...): ...      # SO 10; invoice 6 then 4; order CLOSED
def test_over_invoicing_refused(...): ...                # "Line 1: only 4 left to invoice on SO-00001."
def test_drop_ship_line_moves_no_stock(...): ...         # no StockMovement; no warning even with 0 on hand
def test_negative_stock_warns_then_saves(...): ...
def test_inactive_item_plain_message(...): ...           # Review Focus 3; nothing saved
def test_drop_ship_only_invoice_without_inventory_settings(...): ...   # Review Focus 4
def test_stock_line_without_inventory_settings(...): ...                # "Set up inventory accounts first."
def test_zero_total_invoice_posts_nothing(...): ...
def test_due_date_from_terms(...): ...
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review the posting against the `finance:journal-entry` skill and record findings in the commit body.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: invoices with stock and drop-ship lines`

---

### Task 6: Edit and void invoices (service)

**Files:**
- Modify: `sales/posting/invoices.py`
- Test: `tests/test_invoice_edit_void.py`

**Interfaces:**
- Produces:
  - `edit_invoice(invoice, *, user, date, lines: Sequence[InvoiceLineSpec], terms, ship_to, notes, customer=None, reason="", allow_negative=False) -> Invoice`, per spec 5.3:
    1. Save the snapshot. Its JSON keys are header `customer_id`, `date`, `terms`, `due_date`, `ship_to`, `notes`, and `lines` as a list of `{item_id, sku, description, quantity, price, amount, fulfillment, drop_ship_vendor, so_line_id}`.
    2. `void_entry(old_entry, system=True, date=old_date, reason=f"Edited to revision {n}")`.
    3. `void_movement(m, system=True, date=old_date, ...)` for each old stock movement.
    4. Post the new entry and movements. Increment `revision`.
  - Remaining checks use `remaining(so_line, exclude_invoice=invoice)` (Review Focus 2).
  - `void_invoice(invoice, *, user, reason, date=None) -> Invoice`. Admin only. It is blocked while any active application exists (spec 5.4 message). It reverses the entry and voids the stock movements on the void date, sets `status=VOID`, `voided_on`, `void_reason`, and refreshes the order's status (it may reopen).
- Messages:
  - "This invoice has {applied} applied. The new total can't be less than that."
  - "The customer can't change after payments or credits are applied."
  - "{number} is void and can't be edited."
  - Both dates checked with `assert_open`.

- [ ] **Step 1: Failing tests**

```python
def test_edit_keeps_number_and_snapshot(...): ...           # revision 2; snapshot has old lines
def test_edit_reposts_ledger_and_stock(...): ...             # trial balance and stock reflect only new lines
def test_edit_from_order_counts_own_quantity(...): ...       # Review Focus 2: SO 10, inv A 6, inv B 4; edit A to 6 ok, to 7 refused "only 6 left"
def test_edit_below_applied_refused(...): ...
def test_edit_in_closed_month_refused(...): ...
def test_standard_user_can_edit(standard_user, ...): ...
def test_void_blocked_with_applications_then_allowed(...): ...
def test_void_reopens_closed_order(...): ...
```

- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: edit and void invoices`

---

### Task 7: Payments, credit notes, refunds (service)

**Files:**
- Create: `sales/posting/receipts.py`
- Test: `tests/test_receipts.py`

**Interfaces:**
- Produces:
  - `receive_payment(*, customer, date, method: PaymentMethod, amount: Decimal, user, reference: str = "", bank_account: Account | None = None, applications: Sequence[tuple[Invoice, Decimal]] = ()) -> Payment`:
    - CHECK and CASH deposit to the settings' undeposited account. ACH and WIRE require `bank_account` (subtype BANK): "Choose the bank account the {method} went to."
    - The entry is Dr deposited_to, Cr A/R.
    - Applications get `applied_on=date`.
  - `apply_payment(payment, invoice, amount, *, date, user) -> PaymentApplication`, `remove_application(app, *, date, user) -> None` (works for both application types).
  - `oldest_first(customer, amount) -> list[tuple[Invoice, Decimal]]` (for "Apply oldest first").
  - `@dataclass(frozen=True) CreditLineSpec(description: str, amount: Decimal, account: Account | None = None)`.
  - `create_credit_note(*, customer, date, lines, reason, user) -> CreditNote`. The entry is Dr each line's account (default returns), Cr A/R.
  - `apply_credit(credit_note, invoice, amount, *, date, user) -> CreditApplication`.
  - `issue_refund(*, source: Payment | CreditNote, date, bank_account, method: Literal["CHECK", "ACH"], amount, user, reference: str = "") -> Refund`. Admin only. The entry is Dr A/R, Cr bank.
  - `void_payment`, `void_credit_note`, `void_refund`, each `(doc, *, user, reason, date=None)`, with the spec 5.4 rules. `void_payment` auto-removes its active applications on the void date (Global Constraints).
- Messages:
  - "{invoice} has {balance} left to pay."
  - "This payment has {unapplied} left to apply."
  - "Only {unapplied} is available to refund from {number}."
  - "Enter an amount above zero."
  - "{invoice} belongs to a different customer."
  - The spec 5.4 void messages, verbatim.

- [ ] **Step 1: Failing tests:**
  - A check goes to Undeposited Funds; an ACH needs a bank.
  - Overpayment leaves credit, applied later.
  - Applying more than an invoice's balance is refused.
  - Removing an application keeps history.
  - A credit note posts and applies.
  - A refund from a payment's unapplied amount; a refund above unapplied is refused.
  - Every void rule, including a payment void reopening invoices.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: payments, credit notes, refunds`

---

### Task 8: Deposits (service)

**Files:**
- Create: `sales/posting/deposits.py`
- Test: `tests/test_deposits.py`

**Interfaces:**
- Produces:
  - `undeposited_payments() -> QuerySet[Payment]`: non-void CHECK and CASH payments with no deposit or a void deposit, oldest first.
  - `make_deposit(*, payments: Sequence[Payment], bank_account, date, user) -> Deposit`. The entry is Dr bank, Cr Undeposited for the sum.
  - `void_deposit(deposit, *, user, reason, date=None) -> Deposit`. The payments become undepositable again.
- Messages:
  - "Choose at least one payment."
  - "{number} is already in deposit {deposit}."
  - "{number} was paid by {method} and went straight to the bank."
  - Deposit date before a payment's date: "The deposit date can't be before {number}'s date."

- [ ] **Step 1: Failing tests** (grouping and total; void, then re-deposit; payment void blocked while deposited; each message)
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: deposits`

---

### Task 9: AR and Undeposited checks, close checks

**Files:**
- Create: `sales/checks.py`
- Modify: `sales/apps.py` (`ready()` registers the close checks)
- Test: `tests/test_sales_checks.py`

**Interfaces:**
- Produces:
  - `ar_check(as_of: date | None = None) -> list[str]`, per spec 6.1. The message is "Customer balances say {x}, the A/R account says {y}." (2 dp, thousands separators).
  - `undeposited_check(as_of=None) -> list[str]`. The message is "Undeposited payments say {x}, the Undeposited Funds account says {y}."
  - `close_check(year, month) -> list[str]` runs both at month end, prefixing each problem with "{Month YYYY}: ".
- Both read the ledger with Phase 1 `account_totals`.

- [ ] **Step 1: Failing tests:**
  - Both checks are empty after a mixed scenario.
  - Breaking A/R with a manual entry to the A/R account makes `ar_check` report it, and blocks `close_period` (then the admin override works).
  - A `hypothesis.extra.django.TestCase` property test: random sequences of invoice, edit, payment, application, removal, credit, refund, deposit, and void keep both checks empty at the end and at every as-of date touched.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement.** Review the tie-outs against the `finance:reconciliation` skill and record findings in the commit body.
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** `feat: AR and undeposited funds checks at month close`

---

### Task 10: Sales reports

**Files:**
- Create: `sales/reports.py`, `sales/csv_export.py`, `sales/views/reports.py`, templates
- Modify: `ledger/urls.py` (paths under `reports:`), `core/templates/core/home.html` report links
- Test: `tests/test_sales_reports.py`

**Interfaces:**
- Produces:
  - `ar_aging(as_of) -> Aging(as_of, rows: list[AgingRow(customer, current, d1_30, d31_60, d61_90, over_90, unapplied_credits, total)], totals: AgingRow, ledger_balance, difference)`. Buckets are by `(as_of - due_date).days`: ≤ 0 is current, then 1–30, 31–60, 61–90, > 90. Unapplied payments and credits sit in `unapplied_credits` as a negative number. `total` = buckets + unapplied_credits.
  - `open_invoices(as_of=None, customer=None)`, `customer_balances(as_of=None)`, `open_sales_orders()` (lines with remaining > 0), `undeposited_funds() -> (payments, total, ledger_balance, difference)`, `drop_ship_awaiting_bill()` (non-void invoice drop-ship lines with `drop_ship_billed=False`).
  - URL names: `reports:ar-aging`, `reports:open-invoices`, `reports:customer-balances`, `reports:open-sales-orders`, `reports:undeposited-funds`, `reports:drop-ship-awaiting-bill`. `?format=csv` like the other reports. The AR aging and Undeposited pages show the "Difference" card, and a nonzero difference shows an error banner.

- [ ] **Step 1: Failing tests:**
  - Bucket edges: due today is current; 1 day past due is 1–30; 30 is 1–30; 31 is 31–60; 91 is over 90.
  - As-of before and after a removed application.
  - AR aging total equals A/R.
  - CSV output.
  - A bad date shows "Enter a date like 2026-01-31."
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of all six reports (light and dark), sent.
- [ ] **Step 6: Commit** `feat: sales reports`

---

### Task 11: Invoice PDF

**Files:**
- Create: `sales/pdf.py`, `sales/templates/sales/invoice_pdf.html`
- Modify: `pyproject.toml` and `uv.lock` (`weasyprint>=70,<71`), `Dockerfile` (`libharfbuzz-subset0` added to the apt line), `sales/urls.py`
- Test: `tests/test_invoice_pdf.py`

**Interfaces:**
- Produces:
  - `render_invoice_html(invoice) -> str`.
  - `invoice_pdf(invoice) -> bytes`, using WeasyPrint with `base_url` set so `@font-face` loads the bundled fonts from `core/static/core/fonts/` and the logo from `MEDIA_ROOT`.
  - URL `sales:invoice-pdf` `/sales/invoices/<number>/pdf/` → `application/pdf`, `Content-Disposition: inline; filename="<number>.pdf"`, login required.
- The template matches `docs/design/invoice-mockup.html`: Letter size, 0.6 in margins, the same structure and colors. There is no Google Fonts link. Drop-ship lines carry the note "Shipped directly from the manufacturer". A void invoice gets a diagonal "VOID" watermark. The table header repeats on each page.

- [ ] **Step 1: Failing tests:**
  - The HTML contains the number, customer, line SKUs, "Balance due", and the notes.
  - A drop-ship line has the note.
  - The PDF bytes start with `b"%PDF"` and are more than 2,000 bytes.
  - A 60-line invoice renders, and the HTML has a `<thead>`.
  - A void invoice has "VOID" in the HTML.
  - The view returns `application/pdf`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots:** write demo invoice PDFs (one page, a void one, and the 60-line one) with `invoice_pdf`, rasterize each page with `pdftoppm -png -r 110` (poppler-utils, present in cloud sessions; dev only), compare against `docs/design/invoice-mockup.html`, and send the PNGs plus one PDF.
- [ ] **Step 6: Commit** `feat: invoice PDF with WeasyPrint`

---

### Task 12: Customer and sales settings screens

**Files:**
- Create: `contacts/forms.py`, `contacts/views.py`, `contacts/urls.py`, `contacts/templates/contacts/*`, `sales/forms.py` (settings and numbering forms), `sales/views/settings.py`, `sales/templates/sales/settings.html`, `sales/urls.py`
- Modify: `dnb/urls.py` (`customers/` → contacts, `sales/` → sales), `core/templates/core/base.html` (Sales sidebar group: Customers, Sales orders, Invoices, Payments, Deposits, Credit notes; Setup: Inventory settings, Sales settings)
- Test: `tests/test_customer_views.py`

**Interfaces:**
- Produces the URLs in spec section 8 for customers and `/sales/settings/`. The customer detail page shows: contact info, terms, balance, open invoices (number, date, due, balance, overdue tag), unapplied credits with "Apply" and "Refund" (admin) actions, and history.
- Settings:
  - Accounts filtered by subtype.
  - Company details.
  - Logo upload: PNG or JPEG, ≤ 1 MB, validated by content (Pillow is not a dependency; check the PNG and JPEG magic bytes). Otherwise "Logo must be a PNG or JPEG under 1 MB." and the old logo is kept (Review Focus 5). Served to the settings page by an authenticated view `sales:logo`.
  - Default notes.
  - Numbering table: prefix and next number per type, through `set_sequence`.
- Deactivate a customer with a balance: spec message.

- [ ] **Step 1: Failing tests** (customer CRUD; duplicate name; deactivate rule; settings admin only (403 for Standard); logo validation; numbering conflict message)
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (start `sales/demo.py`: customers and settings; `seed_demo` calls it)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the customer list, customer detail (with open invoices and credits once later tasks add them; empty states now), customer form, and sales settings including a logo error. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: customer and sales settings screens`

---

### Task 13: Sales order and invoice screens

**Files:**
- Create: `sales/views/orders.py`, `sales/views/invoices.py`, templates (`order_list`, `order_form`, `order_detail`, `invoice_from_order`, `invoice_form`, `invoice_list`, `invoice_detail`, `invoice_revision`, `invoice_void`, `invoice_confirm_negative`)
- Modify: `sales/forms.py`, `sales/urls.py`, `sales/demo.py`
- Test: `tests/test_order_invoice_views.py`

**Interfaces:**
- Produces the spec section 8 URLs for orders and invoices.
- Order and invoice forms take up to 30 lines, each with item SKU, quantity, price (pre-filled from `item.sale_price`), description, fulfillment, and vendor. They carry the one-time `token` pattern from Phase 2 adjustments (`items.models.SubmittedToken`), so a double submit saves once.
- A negative-stock warning re-renders a confirm page with "Save anyway", like Phase 2.
- "Invoice a shipment" pre-fills each line's remaining quantity and shows the on-hand count next to stock lines.
- The invoice detail page shows lines, totals, balance, applications (with "Remove" for allowed users), revisions (each opening `invoice_revision`, read-only), journal entry links, and buttons: Edit, Download PDF, Void (admin).

- [ ] **Step 1: Failing tests:**
  - Create an order and invoice a shipment through the views.
  - Over-invoicing shows the message.
  - The negative confirm flow.
  - A double submit posts once.
  - Edit creates revision 2 and the revision page shows the old lines.
  - Void is admin only.
  - An unknown number returns a 404 page in the app's style.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (extend demo: two orders, three invoices, one drop-ship line, one edited invoice)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the order list, order form, order detail (partly invoiced), invoice-a-shipment (with an on-hand warning), negative confirm, invoice list, invoice detail (with revisions), revision view, and the void form. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: sales order and invoice screens`

---

### Task 14: Payment, credit, refund, and deposit screens

**Files:**
- Create: `sales/views/receipts.py`, `sales/views/deposits.py`, templates
- Modify: `sales/forms.py`, `sales/urls.py`, `sales/demo.py`, `contacts/templates/contacts/customer_detail.html` (Apply and Refund actions)
- Test: `tests/test_receipt_views.py`

**Interfaces:**
- Produces the spec section 8 URLs for payments, credit notes, refunds, and deposits.
- **Receive payment:** pick the customer, and their open invoices load (server-rendered after the customer is chosen; no JavaScript framework). The method shows a check-number field and, for ACH or wire, a bank select. "Apply oldest first" fills the amounts via `oldest_first`, and the unapplied amount shows live (small vanilla JavaScript in `sales/static/sales/payment.js`).
- **Deposit:** checkboxes for `undeposited_payments()`, a running total, a bank select, and a date.
- **Credit note:** amount lines with description and account.
- **Refund:** reached from a payment or credit with unapplied value. Admin only.
- All forms use the one-time token.

- [ ] **Step 1: Failing tests:**
  - Payment applied across two invoices.
  - An overpayment shows as credit on the customer page.
  - A deposit removes payments from the undeposited list.
  - A refund over the available amount shows the message.
  - Standard users get 403 on refunds and voids.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (extend demo: payments by check, cash, and ACH; one deposit; one credit note; one refund)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of receive payment (with applications), payment detail, the deposit form and detail, credit note form and detail, the refund form with an error, and the customer detail with credits. Light and dark, sent.
- [ ] **Step 6: Commit** `feat: payment, credit, refund, and deposit screens`

---

### Task 15: Home, acceptance, docs

**Files:**
- Modify: `core/views.py`, `core/templates/core/home.html`, `README.md` (Status, What it does: Sales row)
- Create: `tests/test_phase3_acceptance.py`

- [ ] **Step 1: Home cards.**
  - **Open AR** (total, linking to AR aging).
  - **Overdue** (total, linking to open invoices filtered to overdue).
  - **Undeposited** (total, linking to the deposit form).
  - **Drop shipments awaiting bill** (count, linking to the report).
  - Test: the values for a seeded scenario.
- [ ] **Step 2: Acceptance test** (spec section 1, through the test client as an admin, with checks after each step):
  1. Create an SO with a stock line and a drop-ship line.
  2. Invoice twice.
  3. Edit the first invoice's quantity.
  4. A check payment and an ACH payment.
  5. Deposit the check.
  6. A credit note, applied.
  7. Refund an overpayment.

  After every step, `ar_check() == [] and undeposited_check() == []`. Finally the PDF view returns a PDF.
- [ ] **Step 3: Full suite** `uv run pytest -q` passes with no warnings.
- [ ] **Step 4: Final screenshots:** a tour (home with sales cards, an order, an invoice, the invoice PDF, receive payment, a deposit, AR aging), light and dark, sent.
- [ ] **Step 5: Commit** `test: phase 3 acceptance`
- [ ] **Step 6: Open the PR** `Phase 3: Sales` per `CLAUDE.md`. List the manual checks for the owner:
  - Run `docker compose up -d --build` (new apt library and Python dependency).
  - Fill in Sales settings and upload the logo.
  - Set the next numbers to continue Zoho's sequences.
  - Print one invoice PDF and compare it with a Zoho invoice.

# Adjustments on bills and invoices: plan

Spec: `docs/superpowers/specs/2026-11-19-document-adjustments-design.md`.
PR title: "Adjustment field on bills and invoices"

## Global Constraints
- No new dependencies.
- Money is `Decimal`. All ledger writes go through `ledger/posting.py`; the posting functions here call `post_entry` and `void_entry` only, as today.
- The data migration changes no amount, no journal entry, and no journal line. It only sets flags.
- Balance math (`invoice_total`, `bill_total`, the SQL prefilters) is not changed: an adjustment is a line.
- Existing tests pass untouched, except where a test asserts a bill's or invoice's exact line list and an imported adjustment now shows as the adjustment (note any such change in the PR).
- Error messages match spec section 4.4 exactly.
- Screenshots from a throwaway demo database; never the real `data/` database.

## Review Focus
- The data migration (Task 1): flags only, and only on documents with exactly one matching line.
- Spread math (Task 3): shares sum to the adjustment exactly; stock lines' shares are ignored for order cost; converted Zoho adjustments never spread.
- Edits: the adjustment stays the last line, at most one per document, and an edit that removes it leaves no flagged line.

## Plan-level decisions (mechanical, within the spec)
- Editing a document whose adjustment came from Zoho: if the amount is unchanged, the line keeps `spreads_to_orders=False`; if the amount changes, it is now entered in DnB Books and spreads.
- If the ADJ item does not exist (a new database), the first invoice adjustment creates it: SKU ADJ, name "Adjustment", non-stock, income account from Settings, Inventory "Default income account". If that setting is blank: "Set the default income account in Settings, Inventory first."
- An invoice still needs at least one real line; an adjustment alone is not an invoice.

## Task 1: Flags, setting, and data migration
Interfaces:
```python
# purchasing/models.py
BillExpenseLine.is_adjustment: BooleanField(default=False)
BillExpenseLine.spreads_to_orders: BooleanField(default=False)
BillItemLine.adjustment_share: DecimalField(max_digits=14, decimal_places=2, default=0)
PurchasingSettings.default_adjustment_account: FK Account, null=True, blank=True
# sales/models.py
InvoiceLine.is_adjustment: BooleanField(default=False)
```
- [x] Fields and schema migrations in `purchasing` and `sales`.
- [x] Data migration (`purchasing`): set `default_adjustment_account` to account 5010 when it exists and the setting is blank. Flag a bill's expense line described exactly "Adjustment" when it is the bill's only such line (`spreads_to_orders` stays False).
- [x] Data migration (`sales`): flag an invoice's line on item ADJ or any `ADJ-*` SKU when it is the invoice's only such line.
- [x] Tests `tests/test_adjustments_migration.py`: flags set, single-line rule, totals and every journal line unchanged, setting seeded and not overwritten.
- [x] Commit: `Adjustment flags on bill and invoice lines, with Zoho adjustments converted`

## Task 2: Bill adjustments in posting
Interfaces:
```python
# purchasing/posting/bills.py
@dataclass(frozen=True)
class BillAdjustmentSpec:
    account: Account
    amount: Decimal          # signed, nonzero
    description: str = "Adjustment"
    spreads_to_orders: bool = True

create_bill(..., adjustment: BillAdjustmentSpec | None = None)
bill_from_po(..., adjustment: BillAdjustmentSpec | None = None)
edit_bill(..., adjustment: BillAdjustmentSpec | None = None)   # None removes it
def bill_adjustment(bill: Bill) -> BillExpenseLine | None
```
- [x] Validation (spec 4.4): amount finite, nonzero, at most 2 decimal places; account not inventory or A/P; total of all lines plus adjustment not below zero ("The bill total can't be below zero."), and on edit not below applied (existing message).
- [x] `post_lines` writes the adjustment as the last `BillExpenseLine` with `is_adjustment=True`, and posts it with the other debits (negative credits the account).
- [x] Spread: when `spreads_to_orders`, set each item line's `adjustment_share` in proportion to its amount, rounded to the cent, leftover cent(s) to the largest line (first on ties). Otherwise all shares are 0.
- [x] `_snapshot` records the adjustment (`adjustment: {account_id, account, description, amount, spreads_to_orders}` or null).
- [x] Edit keeps `spreads_to_orders=False` when the old adjustment was a converted one and the amount is unchanged (plan-level decision).
- [x] Zoho replay passes its bill adjustment as `BillAdjustmentSpec(..., spreads_to_orders=False)` when the amount is nonzero.
- [x] Tests `tests/test_bill_adjustments.py`: create and edit with positive, negative, removed; ledger lines; balance; below-zero and below-applied refusals; one adjustment, last; shares sum exactly; replay flagging.
- [x] Commit: `Bill adjustments in posting, with drop-ship shares`

## Task 3: Bill adjustments in drop-ship order cost
- [x] `handling/profit.py`: a drop-ship invoice line's cost from a bill line uses `(bl.amount + bl.adjustment_share) * take / bl.quantity`.
- [x] Tests in `tests/test_bill_adjustments.py`: order profit with a spreading adjustment; mixed stock and drop-ship bill (only the drop-ship share counts); converted adjustment does not change profit; after an edit, the handling owed difference equals the change in profit times the rate.
- [x] Commit: `Bill adjustments count in drop-ship order cost`

## Task 4: Bill screens
- [x] `AdjustmentForm` (amount, account, note) used by the bill form (new, edit) and bill from PO. Blank or 0.00 amount means none. The account preselects `default_adjustment_account`.
- [x] Edit form loads the adjustment into these fields, not into the expense rows.
- [x] Bill page and revision page: adjustment out of the line list; totals read Subtotal, Adjustment (its note), Total.
- [x] Settings, Purchasing: "Default adjustment account".
- [x] Vendor page bill totals are unchanged (already sums all lines).
- [x] Tests `tests/test_bill_adjustments_views.py`.
- [x] Screenshots: bill form with an adjustment, bill form error, bill page, bill from PO, revision page, Purchasing settings.
- [x] Commit: `Adjustment field on bill screens`

## Task 5: Invoice adjustments in posting
Interfaces:
```python
# sales/posting/invoices.py
@dataclass(frozen=True)
class InvoiceAdjustmentSpec:
    amount: Decimal          # signed, nonzero
    description: str = "Adjustment"
    item: Item | None = None # None: the ADJ item

create_invoice(..., adjustment: InvoiceAdjustmentSpec | None = None)
invoice_from_order(..., adjustment: InvoiceAdjustmentSpec | None = None)
edit_invoice(..., adjustment: InvoiceAdjustmentSpec | None = None)   # None removes it
def invoice_adjustment(invoice: Invoice) -> InvoiceLine | None
def adjustment_item() -> Item
```
- [x] Validation (spec 4.4); total not below zero ("The invoice total can't be below zero."), and on edit not below applied (existing message).
- [x] Written as the last `InvoiceLine` (quantity 1, price = amount, `is_adjustment=True`, no stock movement), posted to the item's income account.
- [x] Edit keeps the old adjustment's item when there is one (so `ADJ-<account>` keeps its account).
- [x] `_snapshot` records the adjustment.
- [x] Zoho replay passes its invoice adjustment as `InvoiceAdjustmentSpec(amount, item=<ADJ or ADJ-*>)`.
- [x] Tests `tests/test_invoice_adjustments.py`: create, from order, edit (positive, negative, removed, imported item kept), ledger, balance, refusals, the ADJ item created on a new database, order profit counts the adjustment.
- [x] Commit: `Invoice adjustments in posting`

## Task 6: Invoice screens and PDFs
- [x] Adjustment amount and note on the invoice form (new, edit) and invoice from order. The edit form loads the adjustment into these fields, not the line rows.
- [x] Invoice page, revision page, Classic and Modern PDFs: adjustment out of the line table; totals read Subtotal, Adjustment (its note), Total. Total quantity leaves it out.
- [x] Tests `tests/test_invoice_adjustments_views.py`, including PDF text for both layouts.
- [x] Screenshots: invoice form with an adjustment, form error, invoice page, invoice from order, Classic PDF, Modern PDF, revision page.
- [x] Commit: `Adjustment field on invoice screens and PDFs`

## Task 7: Claude (MCP) tools
- [x] `create_bill`, `edit_bill`: `adjustment: {amount, account?, note?}`. `create_invoice`, `edit_invoice`: `adjustment: {amount, note?}`. On an edit, omitted keeps the current one; `{amount: "0.00"}` removes it.
- [x] `get_bill`, `get_invoice`, and the preview payloads: `subtotal`, `adjustment` (or null), `total`; adjustment lines left out of `expense_lines` and `lines`.
- [x] `edit_bill`: an omitted `item_lines` or `expense_lines` keeps the current lines of that kind; `[]` removes them. Tool description updated.
- [x] Tests `tests/test_mcp_adjustments.py`.
- [x] Commit: `Claude tools: adjustments on bills and invoices; edit_bill keeps omitted lines`

## Task 8: Docs and PR
- [x] README status line and CLAUDE.md pointers.
- [x] Full `uv run pytest -q`.
- [ ] PR into `main` with the title above.

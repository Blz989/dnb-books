# Phase 2: Items and Inventory

- **Date:** 2026-10-27
- **Status:** Draft for review
- **Parent spec:** `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md` (sections 4, 5, 6 apply). This document adds detail to the parent and records decisions made after it. Where they differ, this document wins for Phase 2.
- **Builds on:** Phase 1 ledger core (merged to `main`).

## 1. Goal

Give DNB a working item catalog and stock system: items, stock on hand, inventory value, and every stock change posted to the ledger, so that Phase 3 (sales) and Phase 4 (purchasing) can move stock through the same engine.

### Success criteria (Phase 2 done when)

1. A CSV import of about 50 items with opening stock succeeds, and the Inventory Valuation report total equals the Inventory Asset account balance on the balance sheet.
2. A quantity adjustment, a physical count, and a value adjustment each post the correct journal entry and appear in the item's stock history.
3. Worked FIFO and weighted-average examples, negative stock with later true-up, and a backdated movement all produce the costs given in section 5.
4. Screenshots of every new screen have been sent to the owner (standing rule in `CLAUDE.md`).

## 2. Decisions from the owner

| Topic | Decision |
|---|---|
| Item types | Stock items (counted) and non-stock items (bought or sold, not counted). No service items. |
| Catalog size | 200 to 2,000 active items |
| Units | One unit per item, no conversions. Quantities are whole numbers. |
| Stock tasks | Quantity adjustments, periodic physical counts, value adjustments. No reorder alerts. |
| Negative stock | Warn but allow |
| Costing method | Per item, FIFO or weighted average. New stock items default to FIFO. Locked after the item's first movement. |
| Extra item fields | Category, vendor part number. Preferred vendor is added in Phase 4 when vendor records exist. |
| Screens | Custom screens for everyday work; Django admin stays for setup only |
| Zoho export | Not available now. Rules that depend on Zoho's behavior are marked **Verify before Phase 5**. |

### Out of scope for Phase 2

Reorder levels and alerts, multiple units per item, decimal quantities, item photos, barcodes, service items, multiple warehouses, serial/batch tracking, kits, preferred vendor link (Phase 4), stock movement from sales or purchases (Phases 3 and 4; Phase 2 provides the engine they call).

## 3. Module

New Django app `items` (the name used in the parent spec).

| File | Responsibility |
|---|---|
| `items/models.py` | Category, Item, StockMovement, CostRevision, FifoLayer, StockCount, StockCountLine, InventorySettings |
| `items/costing.py` | Pure costing engine: given an item's method and movements in order, return each movement's correct cost. No database access. |
| `items/stock.py` | The only way stock changes: record a movement, replay costs, post revisions, void. Calls `ledger.posting`. |
| `items/counts.py` | Physical count workflow |
| `items/imports.py` | CSV item import with all-or-nothing validation |
| `items/reports.py` | Inventory valuation, stock movement history, negative stock |
| `items/views.py`, `forms.py`, `urls.py`, `templates/items/` | Custom screens |

Rule carried from Phase 1: **all stock changes go through `items/stock.py`**, just as all ledger writes go through `ledger/posting.py`.

## 4. Data model

| Model | Key fields |
|---|---|
| `Category` | name (unique), is_active |
| `Item` | sku (unique), name, description, item_type (STOCK, NON_STOCK), category (nullable), unit (text, e.g. "each", "box"), sale_price (4 dp), purchase_cost (4 dp), vendor_part_number, costing_method (FIFO, WEIGHTED_AVERAGE; stock items only; default FIFO), income_account, cogs_account (stock only), inventory_account (stock only, must be subtype INVENTORY), is_active |
| `StockMovement` | item, date, kind (OPENING, ADJUSTMENT, COUNT, VALUE_ADJUSTMENT, plus SALE and RECEIPT reserved for Phases 3 and 4), quantity (integer, signed; 0 for value adjustments), unit_cost (4 dp, as costed when saved), total_cost (2 dp, signed), offset_account (the non-inventory side), reason, source_type, source_id, journal_entry, voided (bool), created_by, created_at |
| `CostRevision` | movement, delta (2 dp, signed), journal_entry, created_at. A movement's effective cost is `total_cost` plus the sum of its revisions. |
| `FifoLayer` | item, source movement, original_quantity, remaining_quantity, unit_cost. A cache rebuilt by replay; never edited by hand. |
| `StockCount` | name, scope (ALL or one category), status (OPEN, POSTED, CANCELED), started_at, started_by, posted_at, posted_by |
| `StockCountLine` | count, item, expected_quantity (snapshot at start), counted_quantity (nullable until entered) |
| `InventorySettings` | single row: default inventory, COGS, and income accounts; shrinkage account (quantity adjustments and counts); write-down account (value adjustments); opening balance equity account (opening stock) |

`StockMovement` rows are immutable after save, enforced like Phase 1: model guards plus SQLite triggers. Changes in cost are recorded as `CostRevision` rows, never by editing a movement.

## 5. Costing rules

### 5.1 Movement posting

| Kind | Quantity | Journal entry |
|---|---|---|
| OPENING | + | Dr Inventory, Cr Opening Balance Equity |
| ADJUSTMENT (out) | − | Dr Shrinkage (or chosen account), Cr Inventory |
| ADJUSTMENT (in) | + | Dr Inventory, Cr Shrinkage (or chosen account) |
| COUNT | ± | Same as ADJUSTMENT, offset is the shrinkage account |
| VALUE_ADJUSTMENT | 0 | Write-down: Dr Write-down, Cr Inventory. Write-up: reverse. |
| CostRevision | 0 | Difference between the outflow's account (shrinkage, write-down, or COGS in Phase 3) and Inventory |

Inbound adjustments with no known cost use the item's current unit cost (weighted average or the most recent FIFO layer cost, or `purchase_cost` if the item has never had stock).

**Verify before Phase 5:** how Zoho posts inventory at receipt versus bill, and how credit notes reverse cost. Phase 2 does not need these; Phases 3 and 4 do.

### 5.2 FIFO

Inflows create layers. Outflows consume the oldest remaining layers first. Outflow cost is the sum of consumed layer quantities × layer unit costs.

### 5.3 Weighted average

Each inflow recalculates the average: `(value on hand + inflow value) / (quantity on hand + inflow quantity)`, kept at 4 dp. Outflows are costed at the current average × quantity, rounded to the cent. When quantity on hand reaches exactly zero, any remaining value (rounding cents) is cleared with a CostRevision on that outflow, so no value is stranded.

### 5.4 Negative stock

Outflows beyond quantity on hand are allowed after a warning. The units not on hand are costed at an **estimate**: the item's last known unit cost, or `purchase_cost` if it never had stock. The next inflow first fills the shortfall. The shortfall units' correct cost is the inflow's unit cost (both methods). The difference between estimate and correct cost is posted as a CostRevision on the original outflow.

### 5.5 Replay (covers backdating, negative stock true-ups, and voids)

After any movement is saved or voided, `items/stock.py` replays the item's full movement history in order (date, then id) through `items/costing.py`, compares each outflow's correct cost with its effective cost, and posts a CostRevision for every difference. Each revision is dated on **the affected outflow's own date** when that month is open, so the cost lands in the month of the sale or adjustment. If that month is closed, the revision is dated on the triggering movement's date (always open, since it was just saved). FIFO layers are rebuilt from the replay.

Replaying the full history is acceptable at DNB's scale (hundreds of movements per item). If that ever becomes slow, replay can start from a checkpoint without changing the rules.

### 5.6 Worked examples (become tests)

| # | Method | Movements | Expected |
|---|---|---|---|
| A | FIFO | +10 @ 1.00, +10 @ 1.50, −15 | Outflow cost 17.50; on hand 5 @ 1.50 = 7.50 |
| B | Weighted avg | +10 @ 1.00, +10 @ 1.50, −15 | Average 1.25; outflow 18.75; on hand 5 = 6.25 |
| C | Weighted avg | +3 for a total of 1.00 (unit 0.3333), −1, −1, −1 | Outflows 0.33 each; revision +0.01 on the last outflow; value on hand 0.00 |
| D | FIFO | +2 @ 1.00, −5 (estimate 3 @ 1.00), +10 @ 1.20 | Revision +0.60 on the outflow; on hand 7 @ 1.20 = 8.40 |
| E | FIFO | +10 @ 1.00 (Oct 1), −5 (Oct 10), then backdated +10 @ 0.80 (Sep 25) | Outflow recosted to 4.00; revision −1.00 dated Oct 10; on hand 15 = 4.00 + 10.00 = 14.00 |

Example E: if September were closed, the backdated inflow itself would be blocked by the Phase 1 period lock. If October were closed but September open, the revision would be dated Sep 25 (the triggering movement's date).

### 5.7 Invariants (checked in tests and on the valuation report)

- For every Inventory-subtype account, the sum of effective costs of non-voided movements equals the account's ledger balance.
- Quantity on hand per item equals the sum of non-voided movement quantities.

### 5.8 Change to Phase 1 posting

`ledger.posting.validate_lines` rejects lines on an INVENTORY-subtype account unless the entry's `source_type` starts with `items.`. Message: "Line {n}: inventory accounts can only change through stock movements." `void_entry` refuses entries whose `source_type` starts with `items.`: "This entry was created by a stock movement. Void the movement instead."

## 6. Screens

All custom screens extend `core/base.html`, which gains a navigation bar (Home, Items, Counts, Reports, Admin) and an **Auto / Light / Dark** theme toggle (colors as CSS variables; the choice is remembered per browser).

| Screen | URL | Who | Behavior |
|---|---|---|---|
| Items list | `/items/` | All | Search by SKU, name, vendor part #; filter by category, type, active; columns SKU, name, category, type, on hand, unit cost, value; 50 per page; banner linking to negative stock report when any item is below zero |
| Item detail | `/items/<sku>/` | All | All fields; on hand and value; stock history with date, kind, quantity, unit cost, running balance, journal entry link, user, reason |
| New / edit item | `/items/new/`, `/items/<sku>/edit/` | All | Account defaults from settings; item type and costing method read-only after first movement |
| Quantity adjustment | `/items/adjust/` | All | One or more lines (item, ± quantity, reason, account defaulting to shrinkage), one date; negative-stock warning requires a confirm click |
| Value adjustment | `/items/<sku>/value-adjust/` | Admin | New total value or ± amount, reason; blocked when quantity on hand is zero |
| Counts | `/items/counts/` | All | List; start count (all or one category) snapshots expected quantities; enter counts across sessions; review differences; post as one COUNT movement per changed item; items with movements since the snapshot are flagged and need a confirm |
| Import | `/items/import/` | Admin | Upload CSV, preview, errors by row number, nothing saved unless all rows valid |
| Inventory settings | `/items/settings/` | Admin | Default accounts listed in section 4 |
| Reports | `/reports/inventory-valuation/`, `/reports/stock-movements/`, `/reports/negative-stock/` | All | Valuation as of a date with total and the Inventory Asset balance side by side (difference must be 0.00); movements for a date range and optional item; CSV export like Phase 1 reports |

Home screen gains an Items section and a warning banner when any item is below zero.

### CSV import columns

`sku, name, item_type, category, unit, sale_price, purchase_cost, vendor_part_number, costing_method, opening_quantity, opening_unit_cost, opening_date`

Categories are created if missing. Blank `costing_method` means FIFO. `opening_*` columns are required together or not at all, and only for stock items. All opening stock in one file posts as one OPENING journal entry per date.

## 7. Permissions

| Action | Standard | Admin |
|---|---|---|
| View items, history, reports | ✓ | ✓ |
| Create and edit items, quantity adjustments, counts | ✓ | ✓ |
| Value adjustments, CSV import, inventory settings, void a movement | | ✓ |

## 8. Errors (exact wording is fixed in the plan)

- Negative stock warning: "Line {n}: {sku} has {on_hand} on hand. Saving will take it to {new}. Continue?"
- Deactivate with stock: "{sku} still has {on_hand} on hand. Adjust it to zero before deactivating."
- Change method after movements: "The costing method can't change after an item has stock history."
- Import: "Row {n}: {problem}." for each bad row, for example "Row 14: SKU BOLT-1 already exists."
- Quantity not a whole number: "Line {n}: quantity must be a whole number."

## 9. Testing

| Layer | Proves |
|---|---|
| `items/costing.py` unit tests | Examples A to E in section 5.6, exact to the cent |
| Property test (hypothesis) | Random sequences of inflows, outflows, backdates, and voids keep both invariants in 5.7 |
| Posting tests | Each movement kind produces the entry in 5.1; Phase 1 change in 5.8 blocks manual inventory lines |
| Immutability | Movements cannot be edited through the model or by SQL |
| Count workflow | Snapshot, partial entry, flagged items, posting |
| Import | Valid file posts; any bad row saves nothing and lists every bad row |
| Views | Permissions per section 7; plain-language errors; no 500 on bad input |
| Screenshots | Every screen in section 6, light and dark, sent to the owner |

Accounting review: the `finance:journal-entry` and `finance:reconciliation` skills are used when building `stock.py` and the valuation report.

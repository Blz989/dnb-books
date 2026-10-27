# Phase 2: Items and Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An item catalog with stock on hand, FIFO and weighted-average costing, quantity and value adjustments, physical counts, CSV import, and inventory reports, all posting through the Phase 1 ledger, with every everyday screen (and the Phase 1 home and report pages) in the approved "Ledger paper" design.

**Architecture:** New Django app `items`. A pure costing engine (`items/costing.py`) replays an item's movements and returns correct costs; `items/stock.py` is the only code that changes stock, posts journal entries through `ledger.posting`, and turns cost differences into `CostRevision` rows. Screens are server-rendered Django templates on a rebuilt `core/base.html` with CSS variables for light and dark themes.

**Tech Stack:** Phase 1 stack unchanged (Python 3.12, Django 5.2 LTS, SQLite WAL, uv, pytest, pytest-django, hypothesis). Fonts: Inter and Source Serif 4 as self-hosted woff2 files. Screenshot tooling uses the Node Playwright already installed in cloud containers (dev only, not an app dependency).

**Spec:** `docs/superpowers/specs/2026-10-27-phase-2-items-inventory-design.md` (parent: `docs/superpowers/specs/2026-10-27-zoho-replacement-design.md`). Visual reference: `docs/design/items-mockup.html`.

## Global Constraints

- Everything in `CLAUDE.md` applies, including the standing screenshot rule.
- Money is `Decimal`. Ledger amounts and movement totals: 2 dp. Unit costs, `sale_price`, `purchase_cost`: `DecimalField(max_digits=14, decimal_places=4)`. Rounding is half-up (`ROUND_HALF_UP`) to the cent.
- Quantities are whole numbers (`IntegerField`), signed on movements.
- **Sign convention:** `StockMovement.total_cost` and `CostRevision.delta` are signed changes in inventory value: positive raises inventory, negative lowers it. An outflow costing 17.50 has `total_cost = -17.50`. Spec 5.6's "revision +0.60" (cost of the outflow rises) is `delta = -0.60` here; spec's "revision −1.00" is `delta = +1.00`.
- All stock changes go through `items/stock.py`. All ledger writes still go through `ledger/posting.py`.
- `StockMovement` and `CostRevision` are immutable (model guards plus SQLite triggers), except the one-time void fields on `StockMovement`.
- Journal `source_type` values: `items.movement`, `items.cost_revision`, `items.void`. Source id is the movement or revision pk as a string.
- Every journal entry created here has exactly two lines (inventory account and one offset account). A movement whose total cost is 0.00 posts no entry (`journal_entry = None`).
- Spec clarification (example C): when a weighted-average outflow takes quantity on hand to exactly zero, the engine costs it at all remaining value, so it posts 0.34 directly instead of 0.33 plus a +0.01 revision. The effective cost and the ledger are identical either way.
- UI text, error strings, and URLs exactly as written in the spec and this plan. American English.
- Colors only through the CSS variables in spec 6.1; no hard-coded colors in templates.
- Screenshots: every task that adds or changes a screen ends by running `scripts/screenshots.mjs` against a `seed_demo` database and sending the PNGs (light and dark) with `SendUserFile`.

## Review Focus

1. **Inventory settings not filled in yet** when someone adjusts stock or imports → message "Set up inventory accounts first." with a link to settings, no 500, nothing saved. Pinned in Tasks 5 and 13.
2. **Double-click or browser resubmit on an adjustment** → posts once. Pinned in Task 11 (form carries a one-time token; second submit shows "This adjustment was already saved.").
3. **Backdated change that leaves an earlier value adjustment with no stock to apply to** → blocked, nothing saved, message names the date. Pinned in Task 5.
4. **SKU typed with different case or odd characters** (`bolt-1` vs `BOLT-1`, spaces, slashes) → treated as the same SKU / rejected with the allowed-characters message, in forms and CSV. Pinned in Tasks 2 and 8.
5. **Item with zero cost** (no purchase cost, no history) adjusted in or out → movement saved, no journal entry, invariants still hold. Pinned in Task 5.

---

## File Structure

```
core/static/core/app.css            design tokens (light/dark), layout, components
core/static/core/theme.js           Auto/Light/Dark toggle (localStorage key "dnb-theme")
core/static/core/fonts/             Inter + Source Serif 4 woff2, OFL.txt for each
core/templates/core/base.html       rebuilt: sidebar layout, theme toggle, banners
core/templates/core/home.html       restyled home with summary cards
core/management/commands/seed_demo.py   demo data for screenshots (refuses a non-empty ledger)
scripts/screenshots.mjs             Playwright: log in, capture pages in light and dark
ledger/templates/ledger/*.html      restyled report templates (same context, same URLs)
ledger/posting.py                   + inventory-account rule, + allow_items_source on void_entry
items/
  models.py                         Category, Item, StockMovement, CostRevision, FifoLayer, StockCount, StockCountLine, InventorySettings
  migrations/0002_immutability_triggers.py
  errors.py                         StockError hierarchy
  costing.py                        pure replay engine
  stock.py                          record movements, value adjustments, replay, void, invariants
  counts.py                         count workflow
  imports.py                        CSV parse + apply
  reports.py                        valuation, movements, negative stock
  csv_export.py, forms.py, views.py, urls.py, admin.py
  templates/items/*.html
tests/
  conftest.py                       + items fixtures, + items_purge
  test_design.py, test_items_models.py, test_inventory_posting_rule.py, test_costing.py,
  test_stock.py, test_void_movement.py, test_counts.py, test_imports.py, test_item_reports.py,
  test_item_views.py, test_adjust_views.py, test_count_views.py, test_import_views.py, test_phase2_acceptance.py
```

---

### Task 1: Design system, restyled Phase 1 screens, demo data, screenshot script

**Files:**
- Create: `core/static/core/app.css`, `core/static/core/theme.js`, `core/static/core/fonts/*`, `core/management/commands/seed_demo.py`, `scripts/screenshots.mjs`
- Modify: `core/templates/core/base.html`, `core/templates/core/home.html`, `core/views.py`, `ledger/templates/ledger/*.html`, `README.md` (Development section: how to take screenshots)
- Test: `tests/test_design.py`

**Interfaces:**
- Produces:
  - `base.html` blocks: `title`, `page_title`, `page_subtitle`, `page_actions`, `content`; sidebar `nav_active` context variable (`"home"`, `"items"`, `"adjustments"`, `"counts"`, `"journal"`, `"accounts"`, `"reports"`, `"settings"`); a context processor `core.context_processors.nav` exposing `negative_stock_count` (returns 0 until Task 13 wires it) shown as the sidebar pill "{n} below zero".
  - CSS classes used by every later template: `.card`, `.kpis`/`.kpi`, `.banner.warning|.error|.ok`, `.btn`, `.btn.primary`, `.filters`, `table.data`, `td.num`, `.neg`, `.tag`, `.tag.ns`, `.sku`, `.form-row`, `.field-error`.
  - Theme: `<html data-theme="light|dark">`, attribute absent means Auto. An inline script in `<head>` reads `localStorage["dnb-theme"]` inside `try/catch` before first paint.
  - `seed_demo` command: refuses with "seed_demo only runs on an empty database." if any `JournalEntry` exists; otherwise creates user `demo` / password `demo-pass-123` (Admin group), a chart of accounts, and (after Task 13) inventory settings, about 40 items in 5 categories, opening stock, a few adjustments, one item below zero, and one open count. Each later task extends it for its own screens.
  - `node scripts/screenshots.mjs <base_url> <out_dir> <path> [<path> ...]`: logs in as `demo`, saves `<out_dir>/<slug>-light.png` and `-dark.png` (full page, 1360×860 viewport) per path.
- Fonts: Inter (variable woff2) and Source Serif 4 (600 weight woff2), latin subsets, copied from the npm packages `@fontsource-variable/inter` and `@fontsource/source-serif-4`, with each package's OFL license file next to them. `@font-face` in `app.css`.

- [x] **Step 1: Write failing tests**

```python
def test_base_has_theme_toggle_and_tokens(client, standard_user):
    client.force_login(standard_user)
    html = client.get("/").content.decode()
    assert 'data-theme-choice="auto"' in html and 'data-theme-choice="dark"' in html
    assert "core/app.css" in html

def test_css_defines_tokens_for_both_themes():
    css = Path("core/static/core/app.css").read_text()
    for token in ["--bg", "--surface", "--text", "--muted", "--accent", "--warn-text-strong"]:
        assert css.count(token + ":") >= 2          # light and dark
    assert "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css
    assert "#6f655a" in css                          # spec 6.1 muted (light)

def test_no_hard_coded_colors_in_templates():
    for p in Path(".").glob("*/templates/**/*.html"):
        assert not re.search(r"#[0-9a-fA-F]{3,6}\b", p.read_text()), p

def test_reports_still_work_after_restyle(client, admin_user, sample_ledger):
    client.force_login(admin_user)
    r = client.get("/reports/trial-balance/?as_of=2026-01-31")
    assert r.status_code == 200 and b"11,500.00" in r.content

def test_seed_demo_refuses_non_empty_ledger(posted_entry):
    with pytest.raises(CommandError, match="only runs on an empty database"):
        call_command("seed_demo")
```

All Phase 1 view tests must still pass unchanged.

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement** base layout, CSS, theme script, fonts, restyled home (summary cards: backup status, items below zero placeholder; quick links) and the four report templates (same context variables; tables use `table.data`, numbers `td.num`), `seed_demo`, `screenshots.mjs`, README "Taking screenshots" section.
- [x] **Step 4: Run full suite, expect PASS**
- [x] **Step 5: Screenshots** of `/`, the four reports, and the login page, light and dark. Compare the shell (sidebar, header, toggle, cards) against `docs/design/items-mockup.html` rendered at the same size; send with `SendUserFile`.
- [x] **Step 6: Commit** `feat: Ledger paper design system and restyled home and reports`

---

### Task 2: Items models, immutability, settings, roles

**Files:**
- Create: `items/__init__.py`, `items/apps.py`, `items/models.py`, `items/errors.py`, `items/admin.py`, `items/migrations/0001_initial.py` (generated), `items/migrations/0002_immutability_triggers.py`
- Modify: `dnb/settings.py` (add `items`), `core/management/commands/setup_roles.py`, `tests/conftest.py`
- Test: `tests/test_items_models.py`

**Interfaces:**
- Produces (`items/models.py`):
  - `ItemType` (`STOCK`, `NON_STOCK`), `CostingMethod` (`FIFO`, `WEIGHTED_AVERAGE`), `MovementKind` (`OPENING`, `ADJUSTMENT`, `COUNT`, `VALUE_ADJUSTMENT`, `SALE`, `RECEIPT`).
  - `SKU_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$"`; message "SKU can use letters, numbers, dots, dashes, and underscores (up to 50)."
  - `Category(name unique case-insensitive, is_active)`.
  - `Item` fields per spec section 4; `sku` unique case-insensitively (`UniqueConstraint(Lower("sku"))`), stored as entered. `clean()`: stock items need `cogs_account` (subtype COST_OF_GOODS_SOLD) and `inventory_account` (subtype INVENTORY) and `costing_method`; non-stock items have none of the three; `income_account` subtype INCOME or OTHER_INCOME. Properties: `is_stock -> bool`, `has_movements -> bool`.
  - `StockMovement`: spec fields plus `voided` (bool), `voided_on` (date null), `void_amount` (Decimal 2 dp null), `void_entry` (FK JournalEntry null), `count_line` (FK StockCountLine null). Indexes on `(item, date, id)`.
  - `CostRevision(movement, date, delta, journal_entry null, created_at)`.
  - `FifoLayer(item, source_movement, original_quantity, remaining_quantity, remaining_value)`.
  - `StockCount`, `StockCountLine` per spec; `StockCountLine` unique `(count, item)`.
  - `InventorySettings` single row, all account FKs nullable; `InventorySettings.load() -> InventorySettings`; `is_configured -> bool` (all six set).
- Produces (`items/errors.py`): `StockError(Exception)` with subclasses `SettingsMissingError`, `NegativeStockWarning` (attribute `messages: list[str]`), `CostingError`, `ItemStateError`, `CountError`, `CountNeedsConfirmation` (attribute `skus: list[str]`). CSV import returns its errors as data (Task 8), not exceptions.
- Guards: `StockMovement.save()` on an existing row raises `ItemStateError("Stock movements can't be changed. Void the movement instead.")` unless the only change is `voided` False→True plus setting `voided_on`, `void_amount`, `void_entry`; `delete()` always raises. `CostRevision` save-on-existing and delete raise.
- Triggers (0002, with reverse SQL, same style as `ledger/migrations/0002_immutability_triggers.py`, exposing `FORWARD` and `REVERSE` lists): no DELETE on `items_stockmovement` or `items_costrevision`; no UPDATE on `items_costrevision`; UPDATE on `items_stockmovement` aborts when `OLD.voided = 1` or any column other than `voided`, `voided_on`, `void_amount`, `void_entry_id` changes.
- `setup_roles`: Standard gets add/change/view on `items.item`, `items.category`, `items.stockcount`, `items.stockcountline`, view on `items.stockmovement`, `items.inventorysettings`; `ADMIN_APPS` gains `"items"`.
- Admin: register `Category` and `InventorySettings` only (setup). Items are managed on custom screens.
- `conftest.py`: fixtures `inventory_account` (1300, INVENTORY), `cogs_account` (5000), `shrinkage_account` (6500, EXPENSE "Inventory Shrinkage"), `writedown_account` (6510, EXPENSE "Inventory Write-down"), `opening_equity` (3900, EQUITY "Opening Balance Equity"), `inv_settings` (all six set, income = `revenue`), `make_item(sku, method=FIFO, purchase_cost="1.00")` factory; `items_purge` fixture mirroring `ledger_purge` for items tables (drop items triggers, delete items rows, reinstall), to be listed before `ledger_purge`.

- [x] **Step 1: Write failing tests**

```python
def test_sku_case_insensitive_unique(make_item):
    make_item("BOLT-1")
    with pytest.raises((IntegrityError, ValidationError)):
        make_item("bolt-1")

@pytest.mark.parametrize("sku", ["BOLT 1", "BOLT/1", "-BOLT", ""])
def test_sku_pattern_rejected(sku, make_item):
    with pytest.raises(ValidationError, match="SKU can use letters"):
        make_item(sku)

def test_non_stock_item_has_no_inventory_fields(db, revenue): ...
def test_stock_item_requires_inventory_and_cogs_accounts(db, revenue): ...

def test_movement_cannot_be_edited_by_sql(posted_movement):   # fixture: a movement created by a raw insert for now
    with pytest.raises(DatabaseError):
        StockMovement.objects.filter(pk=posted_movement.pk).update(quantity=5)

def test_movement_void_fields_can_be_set_once(posted_movement): ...
def test_settings_load_is_singleton(db):
    assert InventorySettings.load().pk == InventorySettings.load().pk
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement models, errors, migrations, admin, roles, fixtures**
- [x] **Step 4: Run full suite, expect PASS**
- [x] **Step 5: Commit** `feat: items models with immutable stock movements`

---

### Task 3: Ledger change: inventory accounts only through stock movements

**Files:**
- Modify: `ledger/posting.py`
- Test: `tests/test_inventory_posting_rule.py`

**Interfaces:**
- Produces:
  - `post_entry` and `post_draft` reject any line on an `AccountSubtype.INVENTORY` account unless `source_type.startswith("items.")`: `InvalidLineError("Line {n}: inventory accounts can only change through stock movements.")`. Checked after `validate_lines`, before the period check.
  - `void_entry(entry, *, user, reason, date=None, allow_items_source: bool = False)`: when the entry's `source_type` starts with `items.` and `allow_items_source` is False, raise `EntryStateError("This entry was created by a stock movement. Void the movement instead.")`.
  - The Django admin "Post selected drafts" action surfaces the new message like other `PostingError`s (no admin code change expected; confirm by test).

- [x] **Step 1: Write failing tests**

```python
def test_manual_entry_to_inventory_rejected(admin_user, inventory_account, cash):
    with pytest.raises(InvalidLineError, match="Line 1: inventory accounts can only change through stock movements."):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(inventory_account, debit=D("10.00")), LineSpec(cash, credit=D("10.00"))])

def test_items_source_may_post_to_inventory(admin_user, inventory_account, cash):
    e = post_entry(date=date(2026, 1, 5), user=admin_user, source_type="items.movement", source_id="1", lines=[...])
    assert e.status == EntryStatus.POSTED

def test_void_entry_refuses_items_entries(admin_user, items_entry): ...
def test_admin_post_action_shows_inventory_message(client, admin_user, inventory_draft): ...
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run full suite, expect PASS** (Phase 1 tests unchanged)
- [x] **Step 5: Commit** `feat: inventory accounts change only through stock movements`

---

### Task 4: Costing engine (pure)

**Files:**
- Create: `items/costing.py`
- Test: `tests/test_costing.py` (no database)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Move(id: int, date: date, quantity: int, unit_cost: Decimal | None = None, value_change: Decimal = Decimal("0"))`. Inflows (`quantity > 0`) must carry `unit_cost`; outflows (`quantity < 0`) carry none; value adjustments have `quantity == 0` and a nonzero `value_change`.
  - `@dataclass(frozen=True) CostedMove(id: int, total_cost: Decimal, estimated: bool)` (`total_cost` signed per Global Constraints; `estimated` is True while any of the outflow's units are costed at an estimate).
  - `@dataclass(frozen=True) Layer(source_id: int, original_quantity: int, remaining_quantity: int, remaining_value: Decimal)`.
  - `@dataclass(frozen=True) ReplayResult(costed: dict[int, CostedMove], quantity: int, value: Decimal, layers: tuple[Layer, ...], unit_cost: Decimal)`; `unit_cost` is the current cost per unit at 4 dp (FIFO: newest layer's `remaining_value / remaining_quantity`, else last known; weighted average: current average).
  - `replay(method: CostingMethod, moves: Sequence[Move], fallback_unit_cost: Decimal) -> ReplayResult`. Callers pass moves already sorted by `(date, id)`; the function asserts sorted order.
- Algorithm (decisions the tests depend on):
  - Inflow value is `round_cents(quantity × unit_cost)`.
  - **Shortfall backlog:** an outflow beyond quantity on hand records `(outflow id, short units)` and costs those units at `last_unit_cost × units`, `estimated=True`. `last_unit_cost` starts at `fallback_unit_cost` and updates on every inflow (FIFO: the inflow's unit cost; weighted average: the new average). The next inflows fill the backlog oldest first at the inflow's unit cost; filled units replace the estimate in that outflow's `total_cost`, and `estimated` becomes False once all its short units are filled. Units used to fill the backlog do not create a layer or enter the average.
  - **FIFO:** layers keep `remaining_value`. Taking `k` of a layer's `q` remaining units costs `round_cents(remaining_value × k / q)`, or exactly `remaining_value` when `k == q`.
  - **Weighted average:** state is `(quantity, value, average)`. On inflow: `value += inflow value`, `quantity += q`, `average = (value / quantity)` quantized to 4 dp. Outflow of `k` costs `round_cents(average × k)`, except when it takes quantity to exactly 0, in which case it costs all remaining `value`.
  - **Value adjustment:** requires `quantity > 0` at that point, else `CostingError("The value adjustment on {date} has no stock to apply to.")` (date as `Mon D, YYYY`, e.g. `Oct 25, 2026`). FIFO spreads `value_change` across remaining layers by quantity share (each share rounded to the cent; the newest layer absorbs the remainder). Weighted average adds it to `value` and recomputes `average`.
  - A value adjustment that would make total value negative raises `CostingError("The value adjustment on {date} would make stock value negative.")`.

- [x] **Step 1: Write failing tests** (spec 5.6 examples with the sign convention)

```python
def test_example_a_fifo():
    r = replay(FIFO, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 10, D("1.50")), Move(3, d(3), -15)], D("0"))
    assert r.costed[3].total_cost == D("-17.50") and (r.quantity, r.value) == (5, D("7.50"))

def test_example_b_weighted_average():
    r = replay(WAVG, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 10, D("1.50")), Move(3, d(3), -15)], D("0"))
    assert r.costed[3].total_cost == D("-18.75") and r.value == D("6.25") and r.unit_cost == D("1.2500")

def test_example_c_last_outflow_clears_value():
    r = replay(WAVG, [Move(1, d(1), 3, D("0.3333333333")), Move(2, d(2), -1), Move(3, d(3), -1), Move(4, d(4), -1)], D("0"))
    # inflow value rounds to 1.00
    assert [r.costed[i].total_cost for i in (2, 3, 4)] == [D("-0.33"), D("-0.33"), D("-0.34")] and r.value == D("0.00")

def test_example_d_negative_then_fill():
    before = replay(FIFO, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5)], D("0"))
    assert before.costed[2].total_cost == D("-5.00") and before.costed[2].estimated
    after = replay(FIFO, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5), Move(3, d(3), 10, D("1.20"))], D("0"))
    assert after.costed[2].total_cost == D("-5.60") and not after.costed[2].estimated
    assert (after.quantity, after.value) == (7, D("8.40"))

def test_example_e_backdated_inflow():
    r = replay(FIFO, [Move(9, date(2026, 9, 25), 10, D("0.80")), Move(1, date(2026, 10, 1), 10, D("1.00")),
                      Move(2, date(2026, 10, 10), -5)], D("0"))
    assert r.costed[2].total_cost == D("-4.00") and r.value == D("14.00")

def test_value_adjustment_without_stock_raises(): ...      # "has no stock to apply to"
def test_fifo_value_adjustment_spreads_across_layers(): ...  # layers 10@1.00 and 10@1.50, -5.00 → values 7.50 and 12.50
def test_never_had_stock_uses_fallback(): ...              # outflow -2 with fallback 3.25 → -6.50 estimated

@given(random sequences of moves with both methods)
def test_value_equals_inflows_plus_outflows_plus_adjustments(...): ...
# invariant: r.value == sum(inflow values) + sum(costed outflow totals) + sum(value changes); r.quantity == sum(quantities)
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement `items/costing.py`**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: FIFO and weighted average costing engine`

---

### Task 5: Stock service: movements, posting, negative stock, replay

**Files:**
- Create: `items/stock.py`
- Test: `tests/test_stock.py`

**Interfaces:**
- Consumes: `costing.replay`, `ledger.posting.post_entry`, `ledger.periods.assert_open`, `InventorySettings`, `require_admin`.
- Produces:
  - `@dataclass(frozen=True) StockLine(item: Item, quantity: int, reason: str = "", account: Account | None = None, unit_cost: Decimal | None = None)`.
  - `record_movements(*, kind: MovementKind, date: date, lines: Sequence[StockLine], user, allow_negative: bool = False, source_type: str = "items.movement", source_id: str = "") -> list[StockMovement]`. One movement and one journal entry per line, all in one transaction, then `replay_item` for each touched item.
  - `record_value_adjustment(*, item: Item, date: date, amount: Decimal, reason: str, user, account: Account | None = None) -> StockMovement` (admin only; `amount` signed, 2 dp; offset defaults to write-down account).
  - `replay_item(item: Item, *, trigger_date: date, user) -> list[CostRevision]`.
  - `on_hand(item) -> int`, `stock_value(item) -> Decimal`, `current_unit_cost(item) -> Decimal` (from `replay`; `purchase_cost` fallback).
  - `check_invariants() -> list[str]` (empty when healthy): per INVENTORY account, sum of effective values of its items equals the account's ledger balance; per item, quantity from movements equals `on_hand`. Messages like "1300 Inventory Asset: stock says 49.00, ledger says 50.00."
- Rules:
  - Settings not configured → `SettingsMissingError("Set up inventory accounts first.")`.
  - Non-stock item or inactive item in lines → `ItemStateError("Line {n}: {sku} is not an active stock item.")`. Quantity 0 on an ADJUSTMENT/COUNT line → `ItemStateError("Line {n}: enter a quantity other than zero.")`.
  - Negative check uses on hand **as of the end of all movements** (not just as of the date): if any line takes an item below zero and `allow_negative` is False, raise `NegativeStockWarning` with one message per affected line: "Line {n}: {sku} has {on_hand} on hand. Saving will take it to {new}. Continue?"
  - Inflow lines without `unit_cost` use `current_unit_cost(item)`.
  - Movement's `total_cost` comes from replaying the item with the new movement included. Offset account: line `account` or settings shrinkage account (ADJUSTMENT, COUNT), opening balance equity (OPENING), write-down (VALUE_ADJUSTMENT).
  - Journal entry: `total_cost > 0`: Dr inventory, Cr offset; `< 0`: Dr offset, Cr inventory; `== 0`: no entry. Memo `"{kind label} {sku}: {reason}"`, `source_type="items.movement"`, `source_id=str(movement.pk)`.
  - `replay_item`: replays non-voided movements; for each outflow, `difference = costed.total_cost - effective_total` (effective = `total_cost` + sum of its revisions). Nonzero → `CostRevision(delta=difference)` dated the outflow's date if that month is open, else `trigger_date`; one journal entry per revision (`items.cost_revision`), offset = the outflow's `offset_account`. Rebuilds `FifoLayer` rows from `ReplayResult.layers`. A `CostingError` from replay propagates, so the surrounding transaction rolls back.
  - Concurrency: everything runs inside `transaction.atomic()`; SQLite IMMEDIATE transactions serialize writers (Phase 1 setting).

- [x] **Step 1: Write failing tests**

```python
def test_opening_posts_inventory_vs_opening_equity(inv_settings, admin_user, make_item): ...
def test_adjustment_out_posts_shrinkage(inv_settings, admin_user, item_with_stock): ...
def test_negative_requires_confirmation(inv_settings, admin_user, item_with_stock):  # 10 on hand
    with pytest.raises(NegativeStockWarning) as e:
        record_movements(kind=ADJUSTMENT, date=d, lines=[StockLine(item, -12)], user=admin_user)
    assert e.value.messages == ["Line 1: BOLT-1 has 10 on hand. Saving will take it to -2. Continue?"]
    assert StockMovement.objects.filter(kind=ADJUSTMENT).count() == 0
    record_movements(kind=ADJUSTMENT, date=d, lines=[StockLine(item, -12)], user=admin_user, allow_negative=True)
    assert on_hand(item) == -2

def test_example_d_posts_revision_on_outflow_date(...):
    # FIFO: +2 @1.00 (Oct 1), -5 (Oct 2, allow_negative), then +10 @1.20 (Oct 20)
    rev = CostRevision.objects.get()
    assert rev.delta == D("-0.60") and rev.date == date(2026, 10, 2)
    assert check_invariants() == []

def test_example_e_backdated_inflow_revision(...): ...       # revision delta +1.00 dated Oct 10
def test_revision_dated_on_trigger_when_outflow_month_closed(...): ...
def test_settings_missing_message(admin_user, make_item_without_settings):   # Review Focus 1
    with pytest.raises(SettingsMissingError, match="Set up inventory accounts first."): ...
def test_backdate_breaking_value_adjustment_rolls_back(...):  # Review Focus 3
    # +10 @1.00 Oct 1, value adj -2.00 Oct 5, then backdated -10 on Oct 3 → CostingError "Oct 5, 2026"; nothing new saved
def test_zero_cost_item_posts_no_entry(...):                  # Review Focus 5
    m, = record_movements(kind=ADJUSTMENT, date=d, lines=[StockLine(zero_cost_item, 5)], user=admin_user)
    assert m.journal_entry is None and check_invariants() == []
def test_value_adjustment_admin_only(standard_user, ...): ...

@given(...)  # hypothesis.extra.django.TestCase subclass
def test_random_operations_keep_invariants(...): ...           # opening, adjustments ±, backdates, value adjustments
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement `items/stock.py`.** Before committing, review the posting rules against the `finance:journal-entry` skill and record findings in the commit body.
- [x] **Step 4: Run full suite, expect PASS**
- [x] **Step 5: Commit** `feat: stock movements with posting, negative stock, and cost replay`

---

### Task 6: Void a movement

**Files:**
- Modify: `items/stock.py`
- Test: `tests/test_void_movement.py`

**Interfaces:**
- Produces: `void_movement(movement: StockMovement, *, user, reason: str, date: date | None = None) -> StockMovement`. Admin only; reason required ("A reason is required to void a movement."). Already voided → `ItemStateError("This movement is already voided.")`. Date defaults to the movement's date; closed month raises Phase 1's `PeriodClosedError`.
- Behavior: in one transaction, compute effective value (total + revisions); post a reversing entry (`source_type="items.void"`, memo `"Void of {kind label} {sku}: {reason}"`) for that amount unless 0.00; set `voided`, `voided_on`, `void_amount` (signed, the negation of effective value), `void_entry`; then `replay_item(item, trigger_date=void date)`.
- `stock_value`, `on_hand`, and replay exclude voided movements.

- [x] **Step 1: Write failing tests**

```python
def test_void_inflow_recosts_later_outflow(...):
    # FIFO: +10 @1.00, +10 @2.00, -10 → cost 10.00; void the first inflow → outflow cost 20.00, revision delta -10.00
    assert check_invariants() == []

def test_void_negative_estimate_inflow(...): ...
def test_void_requires_reason_and_admin(...): ...
def test_void_in_closed_month_needs_open_date(...): ...
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: void stock movements with cost replay`

---

### Task 7: Physical counts (service)

**Files:**
- Create: `items/counts.py`
- Test: `tests/test_counts.py`

**Interfaces:**
- Produces:
  - `start_count(*, name: str, category: Category | None, user) -> StockCount`: one line per active stock item in scope with `expected_quantity = on_hand(item)`.
  - `enter_counts(count: StockCount, counts: dict[int, int | None], user) -> None` (item id → counted quantity; `None` clears). Negative counted → `CountError("{sku}: a count can't be negative.")`. Count not OPEN → `CountError("This count is already {status}.")`.
  - `@dataclass(frozen=True) CountDiff(line: StockCountLine, current_on_hand: int, difference: int, moved_since_start: bool)`; `review(count) -> list[CountDiff]` for lines with a counted quantity, `difference = counted - current_on_hand`.
  - `post_count(count, *, date: date, user, confirm_moved: bool = False) -> list[StockMovement]`: if any reviewed line has `moved_since_start` and not `confirm_moved` → `CountNeedsConfirmation(skus)`. Posts one COUNT movement per nonzero difference via `record_movements(..., allow_negative=True)` (counts never go below zero themselves) with reason `"Count: {count.name}"`, links `count_line`, sets status POSTED.
  - `cancel_count(count, user) -> None`.
- `moved_since_start`: any non-voided movement for the item with `created_at > count.started_at` that is not this count's own.

- [x] **Step 1: Write failing tests** (snapshot taken at start; partial entry across two calls; uncounted lines ignored; flagged items require confirm; posting creates COUNT movements with shrinkage entries; posting twice raises "This count is already posted.")
- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: physical count workflow`

---

### Task 8: CSV item import (service)

**Files:**
- Create: `items/imports.py`
- Test: `tests/test_imports.py`

**Interfaces:**
- Produces:
  - `HEADERS = ["sku", "name", "item_type", "category", "unit", "sale_price", "purchase_cost", "vendor_part_number", "costing_method", "opening_quantity", "opening_unit_cost", "opening_date"]`.
  - `@dataclass(frozen=True) RowError(row: int, message: str)` (row numbers count the header as row 1, so the first data row is row 2).
  - `@dataclass(frozen=True) ParsedRow(row: int, sku: str, name: str, item_type: ItemType, category: str, unit: str, sale_price: Decimal, purchase_cost: Decimal, vendor_part_number: str, costing_method: CostingMethod | None, opening_quantity: int | None, opening_unit_cost: Decimal | None, opening_date: date | None)`.
  - `@dataclass(frozen=True) ImportPreview(rows: list[ParsedRow], errors: list[RowError])`.
  - `parse_items_csv(text: str) -> ImportPreview` (UTF-8, optional BOM; accepts `item_type` values `stock`/`non-stock` and `costing_method` values `fifo`/`weighted average`, case-insensitive; blank method on a stock item means FIFO).
  - `apply_import(preview: ImportPreview, *, user) -> list[Item]`: admin only; raises `ValueError` if `preview.errors`; creates categories, items (accounts from settings), then OPENING movements via `record_movements` grouped by `opening_date`, all in one transaction.
- Row error messages (exact):
  - "Row {n}: missing columns: {names}." (header problems are reported once as row 1)
  - "Row {n}: SKU is required." / "Row {n}: SKU can use letters, numbers, dots, dashes, and underscores (up to 50)."
  - "Row {n}: SKU {sku} already exists." (in the database, case-insensitive)
  - "Row {n}: SKU {sku} appears more than once in this file." (case-insensitive)
  - "Row {n}: item_type must be stock or non-stock."
  - "Row {n}: costing_method must be fifo or weighted average." / "Row {n}: non-stock items can't have a costing method or opening stock."
  - "Row {n}: {column} must be a number with at most 4 decimal places."
  - "Row {n}: opening_quantity, opening_unit_cost, and opening_date go together." / "Row {n}: opening_quantity must be a whole number above zero." / "Row {n}: opening_date must look like 2026-10-31." / "Row {n}: opening_date is in a closed month."

- [x] **Step 1: Write failing tests**

```python
def test_valid_file_imports_items_and_opening_stock(admin_user, inv_settings): ...
    # 3 rows, 2 with opening stock on 2026-10-01 → 2 OPENING movements, valuation equals ledger
def test_any_bad_row_saves_nothing_and_lists_every_error(admin_user, inv_settings):
    p = parse_items_csv(BAD)   # rows 3 and 5 bad
    assert [e.row for e in p.errors] == [3, 5]
    with pytest.raises(ValueError): apply_import(p, user=admin_user)
    assert Item.objects.count() == 0
def test_case_insensitive_duplicate_sku_in_file(): ...      # Review Focus 4
def test_bom_and_mixed_case_values(): ...
```

- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement**
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Commit** `feat: CSV item import with all-or-nothing validation`

---

### Task 9: Inventory reports (service, views, CSV)

**Files:**
- Create: `items/reports.py`, `items/csv_export.py`, `items/templates/items/{inventory_valuation,stock_movements,negative_stock}.html`
- Modify: `ledger/urls.py` (add the three report paths under `reports:`), `core/templates/core/home.html` report links
- Test: `tests/test_item_reports.py`

**Interfaces:**
- Produces:
  - `inventory_valuation(as_of: date) -> Valuation(as_of, rows: list[ValuationRow(item, quantity, value)], total: Decimal, ledger_balance: Decimal, difference: Decimal)`. As-of value per item = Σ `total_cost` of movements dated ≤ `as_of` + Σ revision `delta` dated ≤ `as_of` + Σ `void_amount` where `voided_on` ≤ `as_of`. Quantity = Σ quantity of movements dated ≤ `as_of` minus quantities of those voided on or before `as_of`. `ledger_balance` = signed balance of all INVENTORY-subtype accounts as of the date (Phase 1 `account_totals`). Rows with zero quantity and zero value omitted.
  - `stock_movements(start: date, end: date, item: Item | None = None) -> list[MovementRow(movement, effective_value, running_quantity)]` (running quantity per item, ordered by item then date then id).
  - `negative_stock() -> list[NegativeRow(item, quantity, value)]`.
  - URLs: `reports:inventory-valuation` `/reports/inventory-valuation/?as_of=`, `reports:stock-movements` `/reports/stock-movements/?start=&end=&item=<sku>`, `reports:negative-stock` `/reports/negative-stock/`; `&format=csv` like Phase 1 (plain numbers, 2 dp, no separators).
  - Valuation page shows Stock total, Inventory accounts total, and Difference as three `.kpi` cards; a nonzero difference shows an error banner "Stock value and the inventory accounts don't match. Difference: {d}."

- [x] **Step 1: Write failing tests** (valuation equals ledger after a mixed scenario; as-of before a void includes the movement, after excludes it; as-of before a revision date excludes the revision; CSV output; bad date shows "Enter a date like 2026-01-31.")
- [x] **Step 2: Run, expect FAIL**
- [x] **Step 3: Implement.** Review the valuation-to-ledger tie-out against the `finance:reconciliation` skill; record findings in the commit body.
- [x] **Step 4: Run, expect PASS**
- [x] **Step 5: Screenshots** of the three reports (light and dark), sent.
- [x] **Step 6: Commit** `feat: inventory valuation, stock movement, and negative stock reports`

---

### Task 10: Item screens

**Files:**
- Create: `items/urls.py`, `items/views.py` (item views), `items/forms.py` (`ItemForm`), `items/templates/items/{item_list,item_detail,item_form}.html`
- Modify: `dnb/urls.py` (`path("items/", include("items.urls"))`, namespace `items`)
- Test: `tests/test_item_views.py`

**Interfaces:**
- Produces URLs: `items:list` `/items/`, `items:new` `/items/new/`, `items:detail` `/items/<str:sku>/`, `items:edit` `/items/<str:sku>/edit/`, `items:deactivate` `/items/<str:sku>/deactivate/` (POST). SKU lookup is case-insensitive (`sku__iexact`); unknown SKU → 404 page in the app style.
- List: query params `q` (SKU, name, vendor part #, case-insensitive contains), `category` (id), `type` (`stock`/`non-stock`), `inactive=1`; 50 per page (`?page=`); columns and KPI cards exactly as the mockup (Stock value, Stock items, Non-stock items, Below zero); method tag "FIFO" or "Avg"; non-stock shows "Non-stock" tag and em dashes.
- Detail: header with SKU and name, action buttons (Edit, Adjust → `/items/adjust/?item=<sku>`, Value adjust (admin), Deactivate), field summary, stock history table (date, kind, quantity, unit cost, running balance, entry link `JE-000123` to the admin change page, user, reason; voided rows struck through with "Voided" tag; revisions shown as indented rows under their movement).
- `ItemForm`: account fields default from settings; `item_type` and `costing_method` disabled after first movement; method change attempt → "The costing method can't change after an item has stock history."
- Deactivate with stock → "{sku} still has {on_hand} on hand. Adjust it to zero before deactivating."

- [ ] **Step 1: Write failing tests** (search matches vendor part #; pagination; case-insensitive SKU URL; non-stock em dashes; method lock message; deactivate message; standard user can create and edit; history lists revisions)
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (extend `seed_demo` with items, categories, history)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of item list (default, searched, filtered to a category), item detail (FIFO item with history, item below zero, non-stock item), new item form, edit form with locked method; light and dark; compare item list against `docs/design/items-mockup.html`; send.
- [ ] **Step 6: Commit** `feat: item list, detail, and edit screens`

---

### Task 11: Adjustment, value adjustment, and void screens

**Files:**
- Modify: `items/views.py`, `items/forms.py`, `items/urls.py`
- Create: `items/templates/items/{adjust,adjust_confirm,value_adjust,void_movement}.html`
- Test: `tests/test_adjust_views.py`

**Interfaces:**
- Produces URLs: `items:adjust` `/items/adjust/` (`?item=<sku>` pre-fills line 1), `items:value-adjust` `/items/<str:sku>/value-adjust/` (admin), `items:void-movement` `/items/movements/<int:pk>/void/` (admin), and `items:adjustments` `/items/adjustments/` (list of recent ADJUSTMENT and VALUE_ADJUSTMENT movements, newest first, 50 per page; this is the sidebar "Adjustments" target).
- Adjustment form: date (default today), up to 20 lines (item SKU, ± quantity, reason choice `Damaged`/`Lost`/`Sample`/`Correction`/`Other`, optional note, account defaulting to shrinkage), and a hidden `token` (UUID4). On `NegativeStockWarning` the page re-renders as `adjust_confirm.html` listing the messages with "Save anyway" (resubmits with `allow_negative=1` and the same token) and "Go back".
- Duplicate protection (Review Focus 2): tokens are stored on success (in a small `SubmittedToken(token unique, created_at)` model in `items/models.py`, migration 0003); a reused token shows "This adjustment was already saved." and redirects to the item or adjustments list without posting.
- Value adjustment: amount as "New total value" or "Change by", reason required; blocked at zero or negative quantity with "{sku} has no stock to revalue."
- Void: date and reason form; errors shown inline.
- All `StockError`, `PeriodClosedError`, and `SettingsMissingError` messages render in an error banner; the settings message links to `/items/settings/`.

- [ ] **Step 1: Write failing tests** (adjust posts and redirects with a success message; negative shows confirm page and nothing is saved until "Save anyway"; same token twice posts once; standard user gets 403 on value adjust and void; settings-missing banner links to settings; non-whole quantity shows "Line {n}: quantity must be a whole number.")
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of the adjustment form, negative-stock confirmation, success message, value adjustment, void form with an error, adjustments list; light and dark; send.
- [ ] **Step 6: Commit** `feat: adjustment, value adjustment, and void screens`

---

### Task 12: Count screens

**Files:**
- Modify: `items/views.py`, `items/forms.py`, `items/urls.py`
- Create: `items/templates/items/{count_list,count_start,count_enter,count_review}.html`
- Test: `tests/test_count_views.py`

**Interfaces:**
- Produces URLs: `items:counts` `/items/counts/`, `items:count-start` `/items/counts/new/`, `items:count-enter` `/items/counts/<int:pk>/` (paged by 50 lines, filter by SKU/name, saves on each page submit), `items:count-review` `/items/counts/<int:pk>/review/` (differences table, flagged rows marked "Moved since count started", a "I've checked the flagged items" checkbox required when any are flagged, date field, Post button), `items:count-cancel` `/items/counts/<int:pk>/cancel/` (POST).
- Enter screen does **not** show expected quantities (blind count); review does.

- [ ] **Step 1: Write failing tests** (start creates lines; entering on page 2 keeps page 1 values; review shows differences; posting without the checkbox when flagged shows the `CountNeedsConfirmation` SKUs; posted count is read-only)
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (extend `seed_demo` with one open count)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of count list, start, enter, review with a flagged item, posted count; light and dark; send.
- [ ] **Step 6: Commit** `feat: physical count screens`

---

### Task 13: Import and settings screens, home and sidebar wiring

**Files:**
- Modify: `items/views.py`, `items/forms.py`, `items/urls.py`, `core/views.py`, `core/context_processors.py`, `core/templates/core/home.html`
- Create: `items/templates/items/{import_upload,import_preview,settings}.html`
- Test: `tests/test_import_views.py`

**Interfaces:**
- Produces URLs: `items:import` `/items/import/` (admin; upload, max 2 MB, otherwise "The file is too large (2 MB limit)."), `items:import-confirm` (POST; the parsed CSV text is kept in the session between preview and confirm, and the preview is re-parsed on confirm so stale data can't post), `items:settings` `/items/settings/` (admin; six account selects filtered by the right subtype).
- Preview page: counts (items, stock items, items with opening stock, opening value) and either the error list ("Fix these rows and upload again.") or a Confirm button.
- Home: Items card (stock value, below-zero count linking to the negative stock report), Inventory links; warning banner "{n} items are below zero. Their cost is estimated until stock is received or counted." when any; `negative_stock_count` context processor now real.
- Settings not configured: home shows an info banner "Inventory isn't set up yet. Choose the inventory accounts in Settings." for admins.

- [ ] **Step 1: Write failing tests** (standard user 403 on import and settings; preview with errors shows every row error and no confirm button; confirm imports; settings form rejects an account of the wrong subtype; home banner text with 3 below zero; sidebar pill shows "3 below zero")
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** (finish `seed_demo`: settings configured)
- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Screenshots** of home (with and without the below-zero banner), import upload, preview with errors, preview ready, settings; light and dark; send.
- [ ] **Step 6: Commit** `feat: item import and inventory settings screens, home wiring`

---

### Task 14: Phase 2 acceptance

**Files:**
- Create: `tests/test_phase2_acceptance.py`, `tests/fixtures/items_50.csv`
- Modify: `README.md` (Status line: Phase 2 built; "What it does" unchanged)

- [ ] **Step 1: Write the acceptance test** (spec section 1, through the test client as an admin): configure settings, import `items_50.csv` (50 items, 40 with opening stock), confirm valuation report Difference 0.00 and total equals the balance sheet Inventory Asset line; post a quantity adjustment, a count with one difference, and a value adjustment; each appears on its item's history with a journal entry link; `check_invariants() == []`.
- [ ] **Step 2: Run full suite** `uv run pytest -q` → all pass, no warnings.
- [ ] **Step 3: Final screenshots:** full tour (home, items list, item detail, adjustment, count review, valuation report) in light and dark from a fresh `seed_demo`; send.
- [ ] **Step 4: Commit** `test: phase 2 acceptance`
- [ ] **Step 5: Open the PR** `Phase 2: Items and inventory` into `main` per `CLAUDE.md`, listing tasks, test output, deviations, and every screenshot set sent.

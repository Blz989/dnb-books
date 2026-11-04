# Shared drop-ship purchase orders: implementation plan

Spec: `docs/superpowers/specs/2026-11-04-shared-drop-ship-po-design.md`. Branch: `feat/shared-drop-ship-po`.
PR title: `Shared drop-ship purchase orders across sales orders`.

## Global Constraints

- Money is `Decimal`. No ledger writes anywhere in this work (POs post nothing; bills keep posting through
  `purchasing/posting/bills.py` and `ledger/posting.py` unchanged).
- `DropShipAllocation` (bill line to invoice line) stays as it is. The new table is `PurchaseOrderAllocation`
  (PO line to sales order line).
- Matching is always rebuilt from scratch. No manual split screen.
- The Task 1 regression test is written against today's code, must pass before any other change, and must keep
  passing unchanged after every task.
- Tech stack unchanged; no new dependencies.

## Review Focus

- The costing walk: oldest sales order first, overflow to the last allocation, pools sorted by bill, as-of replay
  using the same function as `rebuild`.
- The migration: forward copies every link; reverse restores single links and refuses shared ones.
- Every reader of the old fields is switched (grep for `so_line` on PO lines and `sales_order` on POs is empty).

## Task 1: Regression test on today's code

Files: `tests/test_dropship_regression.py`.

- [x] Build a single-link scenario with today's services: two sales orders (two customers), each with a drop-ship
  line on its own drop-ship PO, a stock line, partial invoices, two partial bills on one PO, a bill void and
  re-bill, an invoice void, and a handling rate. Assert exact `DropShipAllocation` rows, `drop_ship_billed`
  flags, `drop_ship_awaiting_bill()` rows, and `handling.profit.orders()` cost/profit/owed/flags as of today and
  as of two earlier dates.
- [x] Run it on today's code: passes. Commit `Add drop-ship regression test before shared POs`.

## Task 2: Allocation table, migration, and every reader switched

Files: `purchasing/models.py`, `purchasing/migrations/0003_po_allocations.py`, `purchasing/posting/orders.py`,
`purchasing/posting/bills.py`, `purchasing/dropship.py`, `handling/profit.py`, `purchasing/views/orders.py`,
`purchasing/pdf.py`, `purchasing/templates/purchasing/{po_detail,po_pdf,po_list}.html`,
`purchasing/templatetags/purchasing_tags.py`, `mcpserver/read.py`, existing tests that built PO lines with
`so_line=` or read `po.sales_order`; new `tests/test_po_allocation_migration.py`.

Interfaces:

```python
class PurchaseOrderAllocation(models.Model):  # po_line, so_line, quantity; unique (po_line, so_line)
class POLineSpec:  # ... allocations: tuple[tuple[SalesOrderLine, int], ...] = ()  (replaces so_line)
def create_po(*, vendor, date, lines, user, expected_date=None, notes="") -> PurchaseOrder  # no sales_order
PurchaseOrderLine.is_drop_ship -> bool
PurchaseOrder.sales_orders() -> list[SalesOrder]; PurchaseOrder.is_drop_ship -> bool
dropship.bill_pools(so_line_ids, as_of: date | None = None) -> dict[int, list[list]]  # [bill_line, qty]
```

- [x] Migration test first (forward: one allocation of the full PO line quantity per old link, notes kept for an
  unlinked drop-ship PO; reverse: links restored; reverse refuses a shared line). Confirm it fails.
- [x] Model, migration, services, readers. `create_po` checks allocations add up to the line quantity:
  `Line {n}: the sales order quantities must add up to the line quantity.`
- [x] `bill_pools` in this task gives every bill line of a PO line to its allocations in order with no cap
  (all existing data has one allocation); Task 3 adds the cap.
- [x] Full suite and the Task 1 test pass. Commit `Replace the PO to sales order link with allocations`.

## Task 3: Shared costing, oldest sales order first

Files: `purchasing/dropship.py`, `purchasing/posting/bills.py`, `handling/profit.py`, new
`tests/test_shared_dropship.py`.

- [ ] Tests: two customers on one PO line (6 + 4); two bills mixed (4 then 6) fill the older order first; partial
  bills leave the newer order pending; void a bill and the shares move; void an invoice and its share is freed
  for the next invoice of the same order line; editing a bill rebuilds both orders; as-of profit replay on dates
  between the bills. Confirm they fail.
- [ ] Cap each allocation, overflow to the last; bill create/edit/void rebuild every sales order line on the PO
  lines touched. Commit `Allocate shared drop-ship bills oldest sales order first`.

## Task 4: Drop-ship POs from several orders

Files: `purchasing/posting/orders.py`, `tests/test_purchase_orders.py`.

- [ ] Tests: candidates across orders; one PO per vendor across two orders; same item and price combine with one
  allocation per order line; different prices stay separate; unresolved vendor across orders creates nothing;
  single-order call unchanged; editing a PO keeps allocations; single-allocation quantity edit follows; shared
  line quantity edit refused with the spec's message. Confirm they fail.
- [ ] Implement. Commit `Create drop-ship POs from several sales orders`.

## Task 5: Screens

Files: `sales/templates/sales/order_list.html`, `purchasing/urls.py`, `purchasing/views/orders.py`,
`purchasing/templates/purchasing/drop_ship_po.html`, PO detail/PDF templates, `tests/test_po_bill_views.py`.

- [ ] View tests: list has checkboxes and the button; multi-order screen; none checked message; nothing left
  message; POST creates one shared PO; unresolved picker across orders; single-order URL still works; PO detail
  "For" column and subtitle; PDF HTML for one and several orders. Confirm they fail.
- [ ] Implement. Screenshots from a demo database sent with `SendUserFile`. Commit `Multi-select drop-ship PO screen`.

## Task 6: MCP, demos, README

Files: `mcpserver/read.py`, `purchasing/demo.py`, `README.md`, `tests/test_mcp.py`.

- [ ] `get_purchase_order` returns `drop_ship_for` joined and per-line `allocations`; test it.
- [ ] Demo: two more customers' faucet orders from Delta on one shared PO, billed in two partial bills.
- [ ] README: shared drop-ship POs in the purchasing section; Status line.
- [ ] Full suite green. Commit `Shared drop-ship POs in MCP, demo, and README`.

# Shared drop-ship purchase orders

Date: 2026-11-04. Status: approved by the owner (requirements confirmed in the request that started this work).

## Problem

Several sales orders, for different customers, must be able to draw from one vendor purchase order, and several
vendor bills may come in against that PO, mixed across those orders. Today the link is one-to-one:
`PurchaseOrder.sales_order` is one FK and `PurchaseOrderLine.so_line` is one FK to one `SalesOrderLine`. Drop-ship
cost matching (`purchasing/dropship.py`) and the order profit replay (`handling/profit.py`) both depend on that one
link, and handling pays 55% of each order's profit, so the cost allocation must be exact.

## Data model

New model `purchasing.PurchaseOrderAllocation`:

| field | type |
| --- | --- |
| `po_line` | FK `PurchaseOrderLine`, `CASCADE`, `related_name="allocations"` |
| `so_line` | FK `sales.SalesOrderLine`, `PROTECT`, `related_name="po_allocations"` |
| `quantity` | `PositiveIntegerField` (above zero) |

Unique on (`po_line`, `so_line`). Registered with auditlog.

Removed: `PurchaseOrderLine.so_line` and `PurchaseOrder.sales_order`. What they answered is now derived:

- `PurchaseOrderLine.is_drop_ship`: the line has at least one allocation.
- `PurchaseOrder.sales_orders()`: the distinct sales orders its allocations point to, oldest first (date, id).
- `PurchaseOrder.is_drop_ship`: any line is a drop-ship line.
- Sales order to POs: `PurchaseOrder.objects.filter(lines__allocations__so_line__order=order).distinct()`.

A drop-ship PO line's allocations always add up to its quantity. `create_po` checks it.

### Migration (`purchasing/migrations/0003_po_allocations.py`)

1. Create `PurchaseOrderAllocation`.
2. Data: every PO line with `so_line` set becomes one allocation of the PO line's full quantity. A PO whose
   `sales_order` is set but none of whose lines point to that order (a drop-ship PO whose lines lost their link
   when it was edited, see "Fixed along the way") gets `Drop ship for SO-xxxx.` appended to its notes, so the
   link is not lost from the screen.
3. Remove `PurchaseOrderLine.so_line` and `PurchaseOrder.sales_order`.

Reverse: restore the two fields, then set `so_line` from each line's allocation and `sales_order` when all of a
PO's allocations point to one order. A PO line with more than one allocation cannot be expressed in the old schema,
so the reverse stops with `A purchase order line is shared by several sales orders. Undo the sharing first.`
instead of dropping data. Nothing else is touched: invoices, bills, `DropShipAllocation` rows, and ledger entries
are left as they are, so the real dataset (9,684 invoices) is unchanged. The Zoho import never set the old link,
so imported POs get no allocations.

## Creating drop-ship POs

`drop_ship_candidates(orders)` and `create_drop_ship_pos(orders, *, user, date, vendor_choices=None)` take one
sales order or a list of them. The single-order button and screen call the same code with one order.

- Candidates: drop-ship lines with no allocation yet, from every selected order, grouped by the vendor text as
  typed (outer spaces trimmed), as today.
- Each group resolves with `vendor_choices` then `Vendor.find_by_name`; any unresolved name raises
  `UnresolvedVendors` and creates nothing, as today. Groups that resolve to the same vendor share one PO.
- Inside one vendor's PO, lines with the same item and the same price combine into one PO line with the quantities
  summed and one allocation per sales order line. Price is the item's purchase cost, as today. The description
  is the first line's. Lines are ordered by sales order (date, id), then line position.
- One PO per vendor, vendors sorted by name.

### Screens

- Sales order list: a checkbox on each row and a "Create drop-ship PO" button above the table. It opens the
  drop-ship screen for the checked orders (`/purchasing/orders/drop-ship/?order=SO-1&order=SO-2`).
  - No order checked: back to the list with `Select at least one sales order.`
  - None of the checked orders has a drop-ship line left: back to the list with
    `Every drop-ship line on these orders is already on a purchase order.`
- The drop-ship screen lists, per vendor text, each line with its sales order and customer, and the unresolved
  vendor picker as today. It posts back the order numbers as hidden fields.
- The single-order button and URL (`/purchasing/orders/drop-ship/<number>/`) stay and show the same screen.
- PO detail: the subtitle says `drop ship for SO-1, SO-2` with links; drop-ship lines show a "For" column with each
  allocation (order number, customer, quantity).
- PO PDF: one sales order gives today's layout. Several give `Sales Order#: SO-1, SO-2`, a Ship To box reading
  `Several customers (drop ship)` / `See the ship-to list below.`, and a "Ship to" list under the lines with each
  order's customer, address, and items with quantities.
- PO list: the "Drop ship" tag shows for any PO with drop-ship lines.
- Sales order detail: its purchase order list shows every PO with a line allocated to the order.

## Costing (automatic)

For one PO line, bills are walked oldest first (bill date, bill id, line id), open bills only, kind `DROP_SHIP`.
Their quantity fills the line's allocations oldest sales order first (sales order date, sales order id, sales order
line id). Each allocation takes up to its quantity; once every allocation is full, any extra goes to the last one
(so a line with one allocation behaves exactly as before). A bill line can be split across several allocations.

This gives each sales order line a pool of (bill line, quantity) shares, sorted by (bill date, bill id, bill line
id). That pool is matched to the sales order line's invoice lines exactly as today: invoice lines oldest first
(invoice date, invoice id, id), void invoices take nothing, each invoice line takes from the pool in order. One
`DropShipAllocation` row is stored per (bill line, invoice line) taken; `drop_ship_billed` is set when an open
invoice line is fully matched. Cost of a share is `bill_line.amount * quantity / bill_line.quantity`, rounded to
cents per share, as today.

One shared function, `purchasing.dropship.bill_pools(so_line_ids, as_of=None)`, builds the pools. `rebuild` uses
it with every open bill; `handling.profit._drop_ship_matching` uses it with bills dated on or before `as_of`, so
the as-of replay cannot drift from the stored matching.

Rebuild is from scratch, as today:

- Bill created, edited, or voided: rebuild every sales order line allocated on any PO line the bill touches (before
  and after an edit).
- Invoice created, edited, or voided: rebuild the sales order lines its lines (and its saved revisions) point to.
  Pools do not depend on invoices, so other orders sharing the PO are not affected and need no rebuild.

## Editing a PO

- A kept line with one allocation: its allocation's quantity follows the line's new quantity (as today, a
  quantity edit is allowed).
- A kept line with several allocations: the quantity can't change.
  `Line {n} is shared by several sales orders. Its quantity can't change.`
- A removed line (only possible while unbilled) loses its allocations, so its sales order lines can go on a new PO.
- Lines added in the editor have no allocations.

## Everything that read the old link

`purchasing/dropship.py`, `handling/profit.py`, `purchasing/posting/bills.py` (`line_kind`, the rebuild sets),
`purchasing/posting/orders.py`, `purchasing/views/orders.py`, `purchasing/pdf.py` and `po_pdf.html`,
`po_detail.html`, `po_list.html`, `purchasing/templatetags/purchasing_tags.py`, `mcpserver/read.py`
(`get_purchase_order.drop_ship_for` becomes the order numbers joined with ", ", and each drop-ship line gets
`allocations: [{sales_order, customer, quantity}]`), `purchasing/demo.py`, the README. The awaiting-bill report
(`sales/reports.py`) reads `drop_ship_billed` and needs no change. `mcpserver/write.py` and `tools.py` never set
the link and need no change.

## Fixed along the way

`update_po` set `so_line` from the editor's specs, which never carry it, so editing a drop-ship PO silently removed
its link and any later bill was costed as stock or non-stock. With allocations in their own table, an edit keeps
them.

## Out of scope

Splitting shared freight across orders; one bill spanning several POs; a manual cost split screen.

## Tests

Two customers on one PO; two bills mixed across the orders; oldest-first allocation; partial bills; voiding a bill
and an invoice and re-matching; the as-of profit replay; the migration of single-link data (forward and reverse);
unchanged profitability and awaiting-bill totals on single-link data (a regression test written against today's
code before the change, kept passing after it).

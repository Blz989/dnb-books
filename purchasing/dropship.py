"""Drop-ship matching. A vendor bill for a drop-shipped PO line is matched to the customer invoices for the sales
order lines that PO line was bought for. The matching is rebuilt from scratch whenever a bill or invoice touching
the line changes, so it never drifts from the documents."""
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from sales.models import DocStatus, Fulfillment, Invoice, InvoiceLine, InvoiceRevision

from .models import BillItemLine, BillLineKind, DropShipAllocation, PurchaseOrderAllocation


def bill_pools(so_line_ids: Iterable[int], as_of: date | None = None) -> dict[int, list[list]]:
    """For each sales order line, the [bill line, quantity] shares billed for it, oldest bill first.

    Bills on a PO line are walked oldest first and fill its allocations oldest sales order first. With `as_of`,
    only bills dated on or before it count (the profit replay); otherwise every open bill does (the rebuild).
    """
    so_line_ids = set(so_line_ids)
    po_lines = PurchaseOrderAllocation.objects.filter(so_line_id__in=so_line_ids).values("po_line_id")
    allocations: dict[int, list[PurchaseOrderAllocation]] = defaultdict(list)
    for a in PurchaseOrderAllocation.objects.filter(po_line_id__in=po_lines).order_by(
            "so_line__order__date", "so_line__order_id", "so_line_id", "id"):
        allocations[a.po_line_id].append(a)
    bills = BillItemLine.objects.filter(
        kind=BillLineKind.DROP_SHIP, po_line_id__in=po_lines, bill__status=DocStatus.OPEN,
    ).select_related("bill")
    if as_of is not None:
        bills = bills.filter(bill__date__lte=as_of)
    pools: dict[int, list[list]] = defaultdict(list)
    for bl in bills.order_by("bill__date", "bill_id", "id"):
        targets = allocations[bl.po_line_id]
        if targets:
            pools[targets[0].so_line_id].append([bl, bl.quantity])
    return {so_line_id: pools.get(so_line_id, []) for so_line_id in so_line_ids}


def rebuild(so_line_ids: Iterable[int]) -> None:
    so_line_ids = sorted(set(so_line_ids))
    pools = bill_pools(so_line_ids)
    for so_line_id in so_line_ids:
        _rebuild_line(so_line_id, pools[so_line_id])


def _rebuild_line(so_line_id: int, pool: list[list]) -> None:
    DropShipAllocation.objects.filter(invoice_line__so_line_id=so_line_id).delete()

    invoice_lines = list(
        InvoiceLine.objects.filter(so_line_id=so_line_id).select_related("invoice").order_by(
            "invoice__date", "invoice_id", "id")
    )
    for invoice_line in invoice_lines:
        allocated = 0
        if invoice_line.invoice.status == DocStatus.OPEN:
            for entry in pool:
                take = min(entry[1], invoice_line.quantity - allocated)
                if take <= 0:
                    continue
                DropShipAllocation.objects.create(bill_item_line=entry[0], invoice_line=invoice_line, quantity=take)
                entry[1] -= take
                allocated += take
                if allocated == invoice_line.quantity:
                    break
        billed = allocated == invoice_line.quantity and invoice_line.invoice.status == DocStatus.OPEN
        if invoice_line.drop_ship_billed != billed:
            InvoiceLine.objects.filter(pk=invoice_line.pk).update(drop_ship_billed=billed)


def so_lines_for_po_lines(po_line_ids: Iterable[int]) -> set[int]:
    """Every sales order line a bill on these PO lines can cost."""
    return set(PurchaseOrderAllocation.objects.filter(po_line_id__in=set(po_line_ids)).values_list(
        "so_line_id", flat=True))


def on_invoice_changed(invoice: Invoice) -> None:
    """Sales calls this after an invoice is created, edited, or voided. An edit replaces the invoice's lines, so
    the lines it used to have (from its saved revisions) are rebuilt too. Pools don't depend on invoices, so other
    sales orders sharing the PO are not affected."""
    ids = {
        l.so_line_id for l in invoice.lines.all()
        if l.so_line_id and l.fulfillment == Fulfillment.DROP_SHIP
    }
    for revision in InvoiceRevision.objects.filter(invoice=invoice):
        for line in revision.snapshot.get("lines", []):
            if line.get("so_line_id") and line.get("fulfillment") == Fulfillment.DROP_SHIP:
                ids.add(line["so_line_id"])
    rebuild(ids)

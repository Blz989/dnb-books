"""Drop-ship matching. A vendor bill for a drop-shipped line is matched to the customer invoices for the same
sales order line. The matching is rebuilt from scratch whenever a bill or invoice touching the line changes, so
it never drifts from the documents."""
from collections.abc import Iterable

from sales.models import DocStatus, Fulfillment, Invoice, InvoiceLine, InvoiceRevision

from .models import BillItemLine, BillLineKind, DropShipAllocation


def rebuild(so_line_ids: Iterable[int]) -> None:
    for so_line_id in sorted(set(so_line_ids)):
        _rebuild_line(so_line_id)


def _rebuild_line(so_line_id: int) -> None:
    DropShipAllocation.objects.filter(invoice_line__so_line_id=so_line_id).delete()
    DropShipAllocation.objects.filter(bill_item_line__po_line__so_line_id=so_line_id).delete()

    pool = [
        [line, line.quantity]
        for line in BillItemLine.objects.filter(
            kind=BillLineKind.DROP_SHIP, po_line__so_line_id=so_line_id, bill__status=DocStatus.OPEN,
        ).order_by("bill__date", "bill_id", "id")
    ]
    invoice_lines = list(
        InvoiceLine.objects.filter(so_line_id=so_line_id).select_related("invoice").order_by(
            "invoice__date", "invoice_id", "id")
    )
    for invoice_line in invoice_lines:
        if invoice_line.invoice.status != DocStatus.OPEN:
            allocated = 0
        else:
            allocated = 0
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


def on_invoice_changed(invoice: Invoice) -> None:
    """Sales calls this after an invoice is created, edited, or voided. An edit replaces the invoice's lines, so
    the lines it used to have (from its saved revisions) are rebuilt too."""
    ids = {
        l.so_line_id for l in invoice.lines.all()
        if l.so_line_id and l.fulfillment == Fulfillment.DROP_SHIP
    }
    for revision in InvoiceRevision.objects.filter(invoice=invoice):
        for line in revision.snapshot.get("lines", []):
            if line.get("so_line_id") and line.get("fulfillment") == Fulfillment.DROP_SHIP:
                ids.add(line["so_line_id"])
    rebuild(ids)

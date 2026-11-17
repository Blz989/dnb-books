"""Deleting hides a document from the lists and keeps the record (and its audit log) for good.

Bills and invoices must be voided first, so their ledger entries are already reversed. Orders post nothing, but they
can't be deleted while an open invoice or bill still hangs on them. A deleted order is also closed, so it drops out of
the open-order reports and can't be invoiced or billed.
"""
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from core.roles import require_admin
from sales.errors import OrderError
from sales.models import DocStatus, OrderStatus, SalesOrder

from ..errors import PurchasingError
from ..models import Bill, PurchaseOrder, PurchaseOrderAllocation, PurchaseOrderLine


@dataclass
class PoEffect:
    """What deleting a sales order does to one drop-ship PO."""
    po: PurchaseOrder
    lines: list[PurchaseOrderLine] = field(default_factory=list)
    quantities: dict[int, int] = field(default_factory=dict)  # po line id -> quantity released
    deleted_too: bool = False


def delete_bill(bill: Bill, *, user) -> Bill:
    require_admin(user)
    with transaction.atomic():
        current = Bill.objects.get(pk=bill.pk)
        if current.deleted_at:
            raise PurchasingError(f"{current.number} is already deleted.")
        if current.status != DocStatus.VOID:
            raise PurchasingError("Void this bill first. Only a voided bill can be deleted.")
        current.deleted_at = timezone.now()
        current.deleted_by = user
        current.save()
    bill.refresh_from_db()
    return bill


def _open_bill_numbers(po: PurchaseOrder) -> list[str]:
    return list(po.bills.filter(status=DocStatus.OPEN, deleted_at__isnull=True).values_list("number", flat=True))


def po_delete_blocker(po: PurchaseOrder) -> str | None:
    if po.deleted_at:
        return f"{po.number} is already deleted."
    if _open_bill_numbers(po):
        return "Void the bills on this purchase order first."
    return None


def so_delete_blocker(order: SalesOrder) -> str | None:
    if order.deleted_at:
        return f"{order.number} is already deleted."
    if order.invoices.filter(status=DocStatus.OPEN, deleted_at__isnull=True).exists():
        return "Void the invoices on this order first."
    for po in PurchaseOrder.for_sales_order(order):
        numbers = _open_bill_numbers(po)
        if numbers:
            return f"Void the bills on purchase order {po.number} first ({', '.join(numbers)})."
    return None


def so_delete_effects(order: SalesOrder) -> list[PoEffect]:
    """The drop-ship POs this order feeds, and how each changes. The confirm screen shows this."""
    effects = []
    for po in PurchaseOrder.for_sales_order(order).select_related("vendor").order_by("number"):
        effect = PoEffect(po=po)
        for allocation in PurchaseOrderAllocation.objects.filter(
                po_line__order=po, so_line__order=order).select_related("po_line__item"):
            effect.lines.append(allocation.po_line)
            effect.quantities[allocation.po_line_id] = allocation.quantity
        left = sum(l.quantity - effect.quantities.get(l.pk, 0) for l in po.lines.all())
        effect.deleted_too = left <= 0
        effects.append(effect)
    return effects


def _hide(doc, user) -> None:
    doc.status = OrderStatus.CLOSED
    doc.closed_by = user  # a manual close, so the status refresh never reopens it
    doc.closed_at = doc.closed_at or timezone.now()
    doc.deleted_at = timezone.now()
    doc.deleted_by = user
    doc.save()


def delete_po(po: PurchaseOrder, *, user) -> PurchaseOrder:
    """Frees the sales order lines it was bought for, so they can go on another PO."""
    require_admin(user)
    with transaction.atomic():
        current = PurchaseOrder.objects.get(pk=po.pk)
        blocker = po_delete_blocker(current)
        if blocker:
            raise PurchasingError(blocker)
        PurchaseOrderAllocation.objects.filter(po_line__order=current).delete()
        _hide(current, user)
    po.refresh_from_db()
    return po


def delete_sales_order(order: SalesOrder, *, user) -> SalesOrder:
    """Releases this order's share of each drop-ship PO. A PO left with nothing to buy is deleted with it."""
    require_admin(user)
    with transaction.atomic():
        current = SalesOrder.objects.get(pk=order.pk)
        blocker = so_delete_blocker(current)
        if blocker:
            raise OrderError(blocker)
        for effect in so_delete_effects(current):
            PurchaseOrderAllocation.objects.filter(po_line__order=effect.po, so_line__order=current).delete()
            for line in effect.lines:
                line.refresh_from_db()
                line.quantity -= effect.quantities[line.pk]
                if line.quantity <= 0 and not line.bill_lines.exists():
                    line.delete()
                else:
                    line.save()
            if effect.deleted_too:
                _hide(PurchaseOrder.objects.get(pk=effect.po.pk), user)
        _hide(current, user)
    order.refresh_from_db()
    return order

"""Purchase orders. They post nothing; they say what was ordered and what is left to bill."""
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from contacts.models import Vendor
from items.models import Item
from sales.models import DocStatus, DocType, Fulfillment, OrderStatus, SalesOrder, SalesOrderLine
from sales.numbering import next_number

from decimal import Decimal as _D

from ..errors import MAX_PRICE_TEXT, MAX_QUANTITY, PurchasingError, UnresolvedVendors
from ..models import BillItemLine, PurchaseOrder, PurchaseOrderAllocation, PurchaseOrderLine


@dataclass(frozen=True)
class POLineSpec:
    item: Item
    quantity: int
    price: Decimal
    description: str = ""
    # Drop ship: (sales order line, quantity) pairs that add up to `quantity`.
    allocations: tuple[tuple[SalesOrderLine, int], ...] = ()
    line_id: int | None = None


def validate_po_lines(lines: Sequence[POLineSpec]) -> None:
    if not lines:
        raise PurchasingError("A purchase order needs at least one line.")
    for n, line in enumerate(lines, 1):
        if not line.item.is_active:
            raise PurchasingError(f"Line {n}: {line.item.sku} is not an active item.")
        if line.quantity is None or line.quantity <= 0:
            raise PurchasingError(f"Line {n}: enter a quantity above zero.")
        if line.quantity > MAX_QUANTITY:
            raise PurchasingError(f"Line {n}: that quantity is too large.")
        if not isinstance(line.price, Decimal):
            raise TypeError("Prices must be Decimal, never float or int.")
        if not line.price.is_finite():
            raise PurchasingError(f"Line {n}: enter a price like 12.50.")
        if line.price < 0:
            raise PurchasingError(f"Line {n}: price can't be negative.")
        if line.price >= _D(MAX_PRICE_TEXT):
            raise PurchasingError(f"Line {n}: that price is too large.")
        if line.price.as_tuple().exponent < -4:
            raise PurchasingError(f"Line {n}: price can have at most 4 decimal places.")
        if line.allocations and sum(q for _, q in line.allocations) != line.quantity:
            raise PurchasingError(f"Line {n}: the sales order quantities must add up to the line quantity.")


def _line_values(spec: POLineSpec, position: int) -> dict:
    return dict(
        item=spec.item, description=spec.description, quantity=spec.quantity, price=spec.price, position=position,
    )


def create_po(
    *, vendor: Vendor, date: date_type, lines: Sequence[POLineSpec], user, expected_date: date_type | None = None,
    notes: str = "",
) -> PurchaseOrder:
    validate_po_lines(lines)
    if not vendor.is_active:
        raise PurchasingError(f"{vendor.name} is inactive.")
    with transaction.atomic():
        po = PurchaseOrder.objects.create(
            number=next_number(DocType.PO), vendor=vendor, date=date, expected_date=expected_date, notes=notes,
            created_by=user,
        )
        for position, spec in enumerate(lines):
            line = PurchaseOrderLine.objects.create(order=po, **_line_values(spec, position))
            for so_line, quantity in spec.allocations:
                PurchaseOrderAllocation.objects.create(po_line=line, so_line=so_line, quantity=quantity)
        return po


def billed_quantity(line: PurchaseOrderLine, exclude_bill=None) -> int:
    rows = BillItemLine.objects.filter(po_line=line, bill__status=DocStatus.OPEN)
    if exclude_bill is not None:
        rows = rows.exclude(bill=exclude_bill)
    return rows.aggregate(q=Sum("quantity"))["q"] or 0


def po_remaining(line: PurchaseOrderLine, exclude_bill=None) -> int:
    return line.quantity - billed_quantity(line, exclude_bill)


def update_po(
    po: PurchaseOrder, *, lines: Sequence[POLineSpec], user, date: date_type | None = None,
    expected_date: date_type | None = None, notes: str | None = None,
) -> PurchaseOrder:
    validate_po_lines(lines)
    with transaction.atomic():
        current = PurchaseOrder.objects.get(pk=po.pk)
        if current.status == OrderStatus.CLOSED:
            raise PurchasingError(f"{current.number} is closed. Reopen it to make changes.")
        existing = {l.pk: l for l in current.lines.all()}
        kept = set()
        for n, spec in enumerate(lines, 1):
            if spec.line_id is None:
                continue
            line = existing.get(spec.line_id)
            if line is None:
                raise PurchasingError(f"Line {n} doesn't belong to {current.number}.")
            kept.add(line.pk)
            allocations = list(line.allocations.all())
            if len(allocations) > 1 and spec.quantity != line.quantity:
                raise PurchasingError(f"Line {n} is shared by several sales orders. Its quantity can't change.")
            billed = billed_quantity(line)
            if spec.quantity < billed:
                raise PurchasingError(f"Line {n}: {billed} already billed.")
        for n, line in enumerate(current.lines.all(), 1):
            if line.pk not in kept and line.bill_lines.exists():
                raise PurchasingError(f"Line {n} has been billed and can't be removed.")
        for line in existing.values():
            if line.pk not in kept:
                line.delete()
        for position, spec in enumerate(lines):
            values = _line_values(spec, position)
            if spec.line_id is None:
                PurchaseOrderLine.objects.create(order=current, **values)
            else:
                line = existing[spec.line_id]
                for field, value in values.items():
                    setattr(line, field, value)
                line.save()
                allocations = list(line.allocations.all())
                if len(allocations) == 1 and allocations[0].quantity != line.quantity:
                    # A line bought for one sales order line keeps buying all of it for that line.
                    allocations[0].quantity = line.quantity
                    allocations[0].save()
        if date is not None:
            current.date = date
        if expected_date is not None:
            current.expected_date = expected_date
        if notes is not None:
            current.notes = notes
        current.save()
    po.refresh_from_db()
    return po


def close_po(po: PurchaseOrder, user) -> PurchaseOrder:
    po.status = OrderStatus.CLOSED
    po.closed_by = user
    po.closed_at = timezone.now()
    po.save()
    return po


def reopen_po(po: PurchaseOrder, user) -> PurchaseOrder:
    po.status = OrderStatus.OPEN
    po.closed_by = None
    po.closed_at = None
    po.save()
    return po


def refresh_po_status(po: PurchaseOrder) -> None:
    """Close the PO when every line is fully billed; reopen one that closed itself if that stops being true.

    A manual close (closed_by set) is never undone here.
    """
    po.refresh_from_db()
    lines = list(po.lines.all())
    done = bool(lines) and all(po_remaining(l) <= 0 for l in lines)
    if done and po.status == OrderStatus.OPEN:
        po.status = OrderStatus.CLOSED
        po.closed_at = timezone.now()
        po.save()
    elif not done and po.status == OrderStatus.CLOSED and po.closed_by_id is None:
        po.status = OrderStatus.OPEN
        po.closed_at = None
        po.save()


def _orders(orders: SalesOrder | Sequence[SalesOrder]) -> list[SalesOrder]:
    orders = [orders] if isinstance(orders, SalesOrder) else list(orders)
    return sorted({o.pk: o for o in orders}.values(), key=lambda o: (o.date, o.pk))


def drop_ship_candidates(orders: SalesOrder | Sequence[SalesOrder]) -> dict[str, list[SalesOrderLine]]:
    """Drop-ship lines not yet on a PO, grouped by the vendor text as typed (outer spaces trimmed). Lines come
    oldest sales order first, then in line order."""
    groups: dict[str, list[SalesOrderLine]] = defaultdict(list)
    for order in _orders(orders):
        lines = order.lines.filter(
            fulfillment=Fulfillment.DROP_SHIP, po_allocations__isnull=True).select_related("item", "order")
        for line in lines:
            groups[line.drop_ship_vendor.strip()].append(line)
    return dict(groups)


def _combined_specs(lines: list[SalesOrderLine]) -> list[POLineSpec]:
    """One PO line per item and price, with one allocation per sales order line."""
    grouped: dict[tuple[int, Decimal], list[SalesOrderLine]] = {}
    for line in lines:
        grouped.setdefault((line.item_id, line.item.purchase_cost), []).append(line)
    return [
        POLineSpec(same[0].item, sum(l.quantity for l in same), price, description=same[0].description,
                   allocations=tuple((l, l.quantity) for l in same))
        for (_, price), same in grouped.items()
    ]


def create_drop_ship_pos(
    orders: SalesOrder | Sequence[SalesOrder], *, user, date: date_type,
    vendor_choices: dict[str, Vendor] | None = None,
) -> list[PurchaseOrder]:
    """One PO per vendor for the drop-ship lines of these sales orders that are not already on a PO."""
    choices = vendor_choices or {}
    resolved: dict[int, tuple[Vendor, list[SalesOrderLine]]] = {}
    unresolved = []
    for name, lines in drop_ship_candidates(orders).items():
        vendor = choices.get(name) or Vendor.find_by_name(name)
        if vendor is None:
            unresolved.append(name)
            continue
        resolved.setdefault(vendor.pk, (vendor, []))[1].extend(lines)
    if unresolved:
        raise UnresolvedVendors(unresolved)
    with transaction.atomic():
        pos = []
        for vendor, lines in sorted(resolved.values(), key=lambda pair: pair[0].name.casefold()):
            lines.sort(key=lambda l: (l.order.date, l.order_id, l.position, l.pk))
            pos.append(create_po(vendor=vendor, date=date, user=user, lines=_combined_specs(lines)))
        return pos

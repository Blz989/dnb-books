"""Sales orders. Orders post nothing; they only say what was promised and what is left to ship."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from contacts.models import Customer
from items.models import Item

from ..errors import OrderError, SalesError
from ..models import (
    DocStatus, DocType, Fulfillment, Invoice, InvoiceLine, OrderStatus, SalesOrder, SalesOrderLine,
)
from ..numbering import next_number


@dataclass(frozen=True)
class OrderLineSpec:
    item: Item
    quantity: int
    price: Decimal
    description: str = ""
    fulfillment: Fulfillment = Fulfillment.STOCK
    drop_ship_vendor: str = ""
    line_id: int | None = None


def validate_lines(lines: Sequence, error: type[SalesError] = OrderError) -> None:
    """Shared by orders and invoices. Line numbers are 1-based."""
    for n, line in enumerate(lines, 1):
        if not line.item.is_active:
            # The Phase 2 wording, for a stock item that would leave the shelf.
            kind = "stock item" if line.item.is_stock and line.fulfillment == Fulfillment.STOCK else "item"
            raise error(f"Line {n}: {line.item.sku} is not an active {kind}.")
        if line.quantity is None or line.quantity <= 0:
            raise error(f"Line {n}: enter a quantity above zero.")
        if not isinstance(line.price, Decimal):
            raise TypeError("Prices must be Decimal, never float or int.")
        if line.price < 0:
            raise error(f"Line {n}: price can't be negative.")
        if line.price.as_tuple().exponent < -4:
            raise error(f"Line {n}: price can have at most 4 decimal places.")
        if line.fulfillment == Fulfillment.DROP_SHIP and not (line.drop_ship_vendor or "").strip():
            raise error(f"Line {n}: enter the drop-ship vendor.")


def _line_values(spec: OrderLineSpec, position: int) -> dict:
    drop = spec.fulfillment == Fulfillment.DROP_SHIP
    return dict(
        item=spec.item, description=spec.description, quantity=spec.quantity, price=spec.price,
        fulfillment=spec.fulfillment, drop_ship_vendor=spec.drop_ship_vendor.strip() if drop else "",
        position=position,
    )


def create_order(
    *, customer: Customer, date: date_type, lines: Sequence[OrderLineSpec], user,
    ship_to: str | None = None, notes: str = "",
) -> SalesOrder:
    if not lines:
        raise OrderError("An order needs at least one line.")
    with transaction.atomic():
        validate_lines(lines)
        order = SalesOrder.objects.create(
            number=next_number(DocType.SO), customer=customer, date=date, created_by=user, notes=notes,
            ship_to=customer.ship_to_address if ship_to is None else ship_to,
        )
        for position, spec in enumerate(lines):
            SalesOrderLine.objects.create(order=order, **_line_values(spec, position))
        return order


def invoiced_quantity(line: SalesOrderLine, exclude_invoice: Invoice | None = None) -> int:
    rows = InvoiceLine.objects.filter(so_line=line, invoice__status=DocStatus.OPEN)
    if exclude_invoice is not None:
        rows = rows.exclude(invoice=exclude_invoice)
    return rows.aggregate(q=Sum("quantity"))["q"] or 0


def remaining(line: SalesOrderLine, exclude_invoice: Invoice | None = None) -> int:
    return line.quantity - invoiced_quantity(line, exclude_invoice)


def update_order(
    order: SalesOrder, *, lines: Sequence[OrderLineSpec], user, date: date_type | None = None,
    ship_to: str | None = None, notes: str | None = None,
) -> SalesOrder:
    if not lines:
        raise OrderError("An order needs at least one line.")
    with transaction.atomic():
        current = SalesOrder.objects.get(pk=order.pk)
        if current.status == OrderStatus.CLOSED:
            raise OrderError(f"{current.number} is closed. Reopen it to make changes.")
        validate_lines(lines)
        existing = {l.pk: l for l in current.lines.all()}
        kept = set()
        for n, spec in enumerate(lines, 1):
            if spec.line_id is None:
                continue
            line = existing.get(spec.line_id)
            if line is None:
                raise OrderError(f"Line {n} doesn't belong to {current.number}.")
            kept.add(line.pk)
            invoiced = invoiced_quantity(line)
            if spec.quantity < invoiced:
                raise OrderError(f"Line {n}: {invoiced} already invoiced.")
        for n, line in enumerate(current.lines.all(), 1):
            if line.pk not in kept and line.invoice_lines.exists():
                raise OrderError(f"Line {n} has been invoiced and can't be removed.")
        for line in existing.values():
            if line.pk not in kept:
                line.delete()
        for position, spec in enumerate(lines):
            values = _line_values(spec, position)
            if spec.line_id is None:
                SalesOrderLine.objects.create(order=current, **values)
            else:
                line = existing[spec.line_id]
                for field, value in values.items():
                    setattr(line, field, value)
                line.save()
        if date is not None:
            current.date = date
        if ship_to is not None:
            current.ship_to = ship_to
        if notes is not None:
            current.notes = notes
        current.save()
    order.refresh_from_db()
    return order


def close_order(order: SalesOrder, user) -> SalesOrder:
    order.status = OrderStatus.CLOSED
    order.closed_by = user
    order.closed_at = timezone.now()
    order.save()
    return order


def reopen_order(order: SalesOrder, user) -> SalesOrder:
    order.status = OrderStatus.OPEN
    order.closed_by = None
    order.closed_at = None
    order.save()
    return order


def refresh_status(order: SalesOrder) -> None:
    """Close the order when every line is fully invoiced; reopen one that closed itself if that stops being true.

    A manual close (closed_by set) is never undone here.
    """
    order.refresh_from_db()
    lines = list(order.lines.all())
    done = bool(lines) and all(remaining(l) <= 0 for l in lines)
    if done and order.status == OrderStatus.OPEN:
        order.status = OrderStatus.CLOSED
        order.closed_at = timezone.now()
        order.save()
    elif not done and order.status == OrderStatus.CLOSED and order.closed_by_id is None:
        order.status = OrderStatus.OPEN
        order.closed_at = None
        order.save()

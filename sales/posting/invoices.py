"""Invoices: revenue, cost of goods for stock lines, and the stock movements that carry that cost."""
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from contacts.models import Customer, Terms, due_date as terms_due_date
from items.errors import NegativeStockWarning
from items.models import Item, MovementKind
from items.stock import StockLine, record_movements, void_movement
from core.roles import require_admin
from ledger.periods import assert_open
from ledger.posting import LineSpec, post_entry, void_entry

from .. import hooks
from ..errors import InvoiceError
from ..models import (
    DocStatus, DocType, Fulfillment, Invoice, InvoiceLine, InvoiceRevision, OrderStatus, SalesOrder, SalesOrderLine, SalesSettings,
)
from ..balances import invoice_applied
from ..numbering import next_number
from .orders import refresh_status, remaining, validate_lines

CENT = Decimal("0.01")


@dataclass(frozen=True)
class InvoiceLineSpec:
    item: Item
    quantity: int
    price: Decimal
    description: str = ""
    fulfillment: Fulfillment = Fulfillment.STOCK
    drop_ship_vendor: str = ""
    so_line: SalesOrderLine | None = None


def line_amount(spec: InvoiceLineSpec) -> Decimal:
    return (spec.quantity * spec.price).quantize(CENT, rounding=ROUND_HALF_UP)


def _is_stock_line(spec: InvoiceLineSpec) -> bool:
    return spec.item.is_stock and spec.fulfillment == Fulfillment.STOCK


def validate_invoice(
    *, customer: Customer, date: date_type, lines: Sequence[InvoiceLineSpec], sales_order: SalesOrder | None,
    exclude_invoice: Invoice | None = None, check_customer: bool = True, allow_negative_adjustment: bool = False,
) -> None:
    """Every check that can fail before anything is saved."""
    SalesSettings.load().require_configured()
    if check_customer and not customer.is_active:
        raise InvoiceError(f"{customer.name} is inactive.")
    if not lines:
        raise InvoiceError("An invoice needs at least one line with a quantity.")
    validate_lines(lines, error=InvoiceError, allow_negative_price=allow_negative_adjustment)
    assert_open(date)
    if sales_order is not None:
        if sales_order.customer_id != customer.pk:
            raise InvoiceError(f"{sales_order.number} belongs to a different customer.")
        if sales_order.status == OrderStatus.CLOSED and exclude_invoice is None:
            raise InvoiceError(f"{sales_order.number} is closed. Reopen it to invoice it.")
    wanted: dict[int, int] = defaultdict(int)
    for n, spec in enumerate(lines, 1):
        if spec.so_line is None:
            continue
        if sales_order is None or spec.so_line.order_id != sales_order.pk:
            raise InvoiceError(f"Line {n}: that line isn't on the sales order.")
        wanted[spec.so_line.pk] += spec.quantity
        left = remaining(spec.so_line, exclude_invoice=exclude_invoice)
        if wanted[spec.so_line.pk] > left:
            raise InvoiceError(f"Line {n}: only {left - (wanted[spec.so_line.pk] - spec.quantity)} left to invoice on {sales_order.number}.")


def _remap_warning(exc: NegativeStockWarning, stock_positions: list[int]) -> NegativeStockWarning:
    """The stock engine numbers only the stock lines; the user sees invoice line numbers."""
    def fix(message: str) -> str:
        return re.sub(r"^Line (\d+):", lambda m: f"Line {stock_positions[int(m.group(1)) - 1]}:", message)

    return NegativeStockWarning([fix(m) for m in exc.messages])


def _side(account, signed: Decimal) -> LineSpec:
    """Positive is a debit, negative a credit."""
    return LineSpec(account, debit=signed) if signed > 0 else LineSpec(account, credit=-signed)


def post_lines(
    invoice: Invoice, lines: Sequence[InvoiceLineSpec], *, user, allow_negative: bool,
) -> None:
    """Create the invoice lines, record stock movements, and post revenue. Runs inside the caller's transaction."""
    amounts = [line_amount(spec) for spec in lines]
    total = sum(amounts, Decimal("0.00"))

    stock_positions = [n for n, spec in enumerate(lines, 1) if _is_stock_line(spec)]
    movements = []
    if stock_positions:
        try:
            movements = record_movements(
                kind=MovementKind.SALE, date=invoice.date, user=user, allow_negative=allow_negative,
                source_type="sales.invoice", source_id=invoice.number,
                lines=[StockLine(lines[n - 1].item, -lines[n - 1].quantity, account=lines[n - 1].item.cogs_account)
                       for n in stock_positions],
            )
        except NegativeStockWarning as exc:
            raise _remap_warning(exc, stock_positions) from None
    movement_for = {n: m for n, m in zip(stock_positions, movements)}

    for n, (spec, amount) in enumerate(zip(lines, amounts), 1):
        drop = spec.fulfillment == Fulfillment.DROP_SHIP
        InvoiceLine.objects.create(
            invoice=invoice, item=spec.item, description=spec.description, quantity=spec.quantity,
            price=spec.price, amount=amount, so_line=spec.so_line, fulfillment=spec.fulfillment,
            drop_ship_vendor=spec.drop_ship_vendor.strip() if drop else "", stock_movement=movement_for.get(n),
            position=n - 1,
        )

    entry = None
    if total != 0:
        by_account: dict = defaultdict(lambda: Decimal("0.00"))
        for spec, amount in zip(lines, amounts):
            by_account[spec.item.income_account] += amount
        settings = SalesSettings.load()
        entry = post_entry(
            date=invoice.date, user=user, memo=f"Invoice {invoice.number} {invoice.customer.name}",
            source_type="sales.invoice", source_id=invoice.number,
            # A negative net (only possible with allow_negative_adjustment) posts on the other side.
            lines=[_side(settings.ar_account, total)] + [
                _side(account, -amount) for account, amount in by_account.items() if amount != 0
            ],
        )
    invoice.journal_entry = entry
    invoice.save()


def create_invoice(
    *, customer: Customer, date: date_type, lines: Sequence[InvoiceLineSpec], user,
    sales_order: SalesOrder | None = None, terms: Terms | None = None, ship_to: str | None = None,
    notes: str | None = None, allow_negative: bool = False, allow_negative_adjustment: bool = False,
) -> Invoice:
    """allow_negative_adjustment lets non-stock lines have negative prices; only the Zoho import passes it."""
    with transaction.atomic():
        validate_invoice(
            customer=customer, date=date, lines=lines, sales_order=sales_order,
            allow_negative_adjustment=allow_negative_adjustment)
        terms = terms or customer.terms
        invoice = Invoice.objects.create(
            number=next_number(DocType.INV), customer=customer, date=date, terms=terms,
            due_date=terms_due_date(terms, date), sales_order=sales_order, created_by=user,
            ship_to=(sales_order.ship_to if sales_order else customer.ship_to_address) if ship_to is None else ship_to,
            notes=SalesSettings.load().default_invoice_notes if notes is None else notes,
        )
        post_lines(invoice, lines, user=user, allow_negative=allow_negative)
        if sales_order is not None:
            refresh_status(sales_order)
        hooks.invoice_changed(invoice)
        return invoice


def invoice_from_order(
    order: SalesOrder, *, date: date_type, quantities: dict[int, int], user, allow_negative: bool = False,
    ship_to: str | None = None, notes: str | None = None, terms: Terms | None = None,
) -> Invoice:
    by_id = {l.pk: l for l in order.lines.select_related("item")}
    unknown = set(quantities) - set(by_id)
    if unknown:
        raise InvoiceError(f"A line doesn't belong to {order.number}.")
    specs = [
        InvoiceLineSpec(
            line.item, quantities[line.pk], line.price, description=line.description,
            fulfillment=line.fulfillment, drop_ship_vendor=line.drop_ship_vendor, so_line=line,
        )
        for line in by_id.values() if quantities.get(line.pk, 0) != 0
    ]
    return create_invoice(
        customer=order.customer, date=date, lines=specs, user=user, sales_order=order, ship_to=ship_to, notes=notes,
        terms=terms, allow_negative=allow_negative,
    )


def _snapshot(invoice: Invoice) -> dict:
    return {
        "customer_id": invoice.customer_id, "date": invoice.date.isoformat(), "terms": invoice.terms,
        "due_date": invoice.due_date.isoformat(), "ship_to": invoice.ship_to, "notes": invoice.notes,
        "lines": [
            {
                "item_id": l.item_id, "sku": l.item.sku, "description": l.description, "quantity": l.quantity,
                "price": str(l.price), "amount": str(l.amount), "fulfillment": l.fulfillment,
                "drop_ship_vendor": l.drop_ship_vendor, "so_line_id": l.so_line_id,
            }
            for l in invoice.lines.select_related("item")
        ],
    }


def _reverse_posting(invoice: Invoice, *, user, when: date_type, reason: str) -> None:
    """Reverse the invoice's revenue entry and stock movements as of `when`. These are system voids: a Standard
    user's edit does them on their behalf, and no view can ask for system=True."""
    if invoice.journal_entry_id:
        void_entry(invoice.journal_entry, user=user, reason=reason, date=when, system=True)
    for line in invoice.lines.select_related("stock_movement"):
        if line.stock_movement_id:
            void_movement(line.stock_movement, user=user, reason=reason, date=when, system=True)


def edit_invoice(
    invoice: Invoice, *, user, date: date_type, lines: Sequence[InvoiceLineSpec], terms: Terms | None,
    ship_to: str, notes: str, customer: Customer | None = None, reason: str = "", allow_negative: bool = False,
) -> Invoice:
    with transaction.atomic():
        current = Invoice.objects.select_related("customer", "sales_order", "journal_entry").get(pk=invoice.pk)
        if current.status == DocStatus.VOID:
            raise InvoiceError(f"{current.number} is void and can't be edited.")
        new_customer = customer or current.customer
        changed_customer = new_customer.pk != current.customer_id
        if changed_customer and (
            current.payment_applications.filter(removed=False).exists()
            or current.credit_applications.filter(removed=False).exists()
        ):
            raise InvoiceError("The customer can't change after payments or credits are applied.")
        assert_open(current.date)
        first_applied = min(
            [a.applied_on for a in current.payment_applications.all()]
            + [a.applied_on for a in current.credit_applications.all()], default=None)
        if first_applied is not None and date > first_applied:
            raise InvoiceError(
                f"The invoice date can't be after {first_applied.isoformat()}, when a payment or credit was applied.")
        validate_invoice(
            customer=new_customer, date=date, lines=lines, sales_order=current.sales_order,
            exclude_invoice=current, check_customer=changed_customer,
        )
        new_total = sum((line_amount(spec) for spec in lines), Decimal("0.00"))
        applied = invoice_applied(current)
        if new_total < applied:
            raise InvoiceError(
                f"This invoice has {applied:,.2f} applied. The new total can't be less than that.")

        InvoiceRevision.objects.create(
            invoice=current, revision=current.revision, snapshot=_snapshot(current), edited_by=user, reason=reason)
        _reverse_posting(
            current, user=user, when=current.date, reason=f"Edited to revision {current.revision + 1}")
        current.lines.all().delete()

        terms = terms or current.terms
        current.customer = new_customer
        current.date = date
        current.terms = terms
        current.due_date = terms_due_date(terms, date)
        current.ship_to = ship_to
        current.notes = notes
        current.revision += 1
        current.journal_entry = None
        current.save()
        post_lines(current, lines, user=user, allow_negative=allow_negative)
        if current.sales_order_id:
            refresh_status(current.sales_order)
        hooks.invoice_changed(current)
    invoice.refresh_from_db()
    return invoice


def void_invoice(invoice: Invoice, *, user, reason: str, date: date_type | None = None) -> Invoice:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("Enter a reason.")
    with transaction.atomic():
        current = Invoice.objects.select_related("journal_entry").get(pk=invoice.pk)
        if current.status == DocStatus.VOID:
            raise InvoiceError(f"{current.number} is already void.")
        if (
            current.payment_applications.filter(removed=False).exists()
            or current.credit_applications.filter(removed=False).exists()
        ):
            raise InvoiceError(f"{current.number} has payments or credits applied. Remove them first.")
        removed = [a.removed_on for a in [*current.payment_applications.all(), *current.credit_applications.all()]
                   if a.removed_on]
        when = date or max([current.date, *removed])
        if when < current.date:
            raise InvoiceError("The void date can't be before the invoice date.")
        if removed and when < max(removed):
            raise InvoiceError(f"The void date can't be before {max(removed).isoformat()}, when a payment or credit was removed.")
        assert_open(when)
        _reverse_posting(current, user=user, when=when, reason=reason)
        current.status = DocStatus.VOID
        current.voided_on = when
        current.void_reason = reason
        current.save()
        if current.sales_order_id:
            refresh_status(current.sales_order)
        hooks.invoice_changed(current)
    invoice.refresh_from_db()
    return invoice

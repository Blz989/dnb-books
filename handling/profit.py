"""Profit per order (spec 5.1). Reads documents only and posts nothing.

An order is a sales order with every invoice linked to it, or a direct invoice with no sales order. Everything is
computed as of a date: only documents dated on or before `as_of` count.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from django.db.models import Prefetch, Q, Sum

from ledger.balances import money
from purchasing.models import DropShipAllocation
from sales.models import CreditApplication, DocStatus, Fulfillment, Invoice, InvoiceLine, SalesOrder

from .models import HandlingRate, HandlingRunLine, HandlingSettings, RunStatus

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

FLAG_PENDING = "Drop-ship bill not entered"
FLAG_LOSS = "Below zero, no handling"
FLAG_BEFORE_START = "Before handling start"


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class LineCost:
    invoice_line: InvoiceLine
    kind: Literal["stock", "drop_ship"]
    cost: Decimal
    pending: bool


@dataclass
class InvoiceProfit:
    invoice: Invoice
    sales: Decimal
    credits: Decimal
    lines: list[LineCost]

    @property
    def cost(self) -> Decimal:
        return sum((l.cost for l in self.lines), ZERO)


@dataclass
class OrderProfit:
    key: str
    sales_order: SalesOrder | None
    invoice: Invoice | None
    customer: object
    date: date
    sales: Decimal
    credits: Decimal
    cost: Decimal
    profit: Decimal
    rate: Decimal | None
    owed: Decimal
    billed: Decimal
    flags: list[str] = field(default_factory=list)
    invoices: list[InvoiceProfit] = field(default_factory=list)

    @property
    def net_sales(self) -> Decimal:
        return self.sales - self.credits

    @property
    def margin(self) -> Decimal | None:
        if self.net_sales == 0:
            return None
        return (self.profit / self.net_sales * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @property
    def balance(self) -> Decimal:
        return self.owed - self.billed


# ---- reads ----

def _invoices(as_of: date):
    """Invoices dated on or before as_of, with everything the profit needs fetched in a fixed number of queries."""
    return (
        Invoice.objects.filter(date__lte=as_of)
        .select_related("customer", "sales_order", "sales_order__customer")
        .prefetch_related(
            Prefetch("lines", queryset=InvoiceLine.objects.select_related("stock_movement").prefetch_related(
                "stock_movement__revisions",
                Prefetch("drop_ship_allocations",
                         queryset=DropShipAllocation.objects.select_related("bill_item_line__bill")),
            )),
            Prefetch("credit_applications", queryset=CreditApplication.objects.filter(applied_on__lte=as_of)),
        )
        .order_by("date", "id")
    )


def _rates() -> list[HandlingRate]:
    return list(HandlingRate.objects.order_by("-effective_from"))


def _rate_on(rates: list[HandlingRate], day: date) -> Decimal | None:
    # Same rule as rates.rate_for, over rates already loaded, so a page of orders costs one query.
    for r in rates:
        if r.effective_from <= day:
            return r.rate
    return None


def _billed(keys) -> dict[str, Decimal]:
    rows = (
        HandlingRunLine.objects.filter(order_key__in=list(keys), run__status=RunStatus.POSTED)
        .values("order_key").annotate(s=Sum("this_bill"))
    )
    return {r["order_key"]: money(r["s"]) for r in rows}


def billed_to_date(key: str) -> Decimal:
    return _billed([key]).get(key, ZERO)


# ---- profit ----

def _stock_cost(line: InvoiceLine, as_of: date) -> Decimal:
    m = line.stock_movement
    if m.voided and m.voided_on is not None and m.voided_on <= as_of:
        return ZERO
    # items.stock.effective_total over the prefetched revisions (it would run one query per movement).
    # A sale's movement is negative, so its cost is the negated effective total.
    return -(m.total_cost + sum((r.delta for r in m.revisions.all()), ZERO))


def _line_cost(line: InvoiceLine, as_of: date) -> LineCost:
    if line.fulfillment == Fulfillment.DROP_SHIP:
        cost, allocated = ZERO, 0
        for a in line.drop_ship_allocations.all():
            bill_line = a.bill_item_line
            bill = bill_line.bill
            if bill.status != DocStatus.OPEN or bill.date > as_of:
                continue
            cost += _cents(bill_line.amount * a.quantity / bill_line.quantity)
            allocated += a.quantity
        return LineCost(line, "drop_ship", cost, allocated < line.quantity)
    cost = _stock_cost(line, as_of) if line.stock_movement_id else ZERO
    return LineCost(line, "stock", cost, False)


def _invoice_profit(invoice: Invoice, as_of: date) -> InvoiceProfit:
    if invoice.status == DocStatus.VOID and invoice.voided_on is not None and invoice.voided_on <= as_of:
        return InvoiceProfit(invoice, ZERO, ZERO, [LineCost(l, _kind(l), ZERO, False) for l in invoice.lines.all()])
    lines = list(invoice.lines.all())
    sales = sum((l.amount for l in lines), ZERO)
    credits = sum(
        (a.amount for a in invoice.credit_applications.all()
         if not (a.removed and a.removed_on is not None and a.removed_on <= as_of)),
        ZERO,
    )
    return InvoiceProfit(invoice, sales, credits, [_line_cost(l, as_of) for l in lines])


def _kind(line: InvoiceLine) -> Literal["stock", "drop_ship"]:
    return "drop_ship" if line.fulfillment == Fulfillment.DROP_SHIP else "stock"


def _order_profit(key: str, sales_order, direct, invoices, as_of, *, settings, rates, billed) -> OrderProfit:
    head = sales_order or direct
    order_date = head.date
    parts = [_invoice_profit(i, as_of) for i in invoices]
    sales = sum((p.sales for p in parts), ZERO)
    credits = sum((p.credits for p in parts), ZERO)
    cost = sum((p.cost for p in parts), ZERO)
    profit = sales - credits - cost
    rate = _rate_on(rates, order_date)

    flags = []
    if any(l.pending for p in parts for l in p.lines):
        flags.append(FLAG_PENDING)
    if profit < 0:
        flags.append(FLAG_LOSS)
    before_start = settings.start_date is not None and order_date < settings.start_date
    if before_start:
        flags.append(FLAG_BEFORE_START)

    if before_start or rate is None:
        owed = ZERO
    else:
        owed = max(ZERO, _cents(profit * rate))

    return OrderProfit(
        key=key, sales_order=sales_order, invoice=direct, customer=head.customer, date=order_date,
        sales=sales, credits=credits, cost=cost, profit=profit, rate=rate, owed=owed, billed=billed,
        flags=flags, invoices=parts,
    )


def order_profit(key: str, as_of: date) -> OrderProfit:
    """One order by key: a sales order number, or the number of an invoice with no sales order."""
    so = SalesOrder.objects.select_related("customer").filter(number=key).first()
    if so is not None:
        invoices = list(_invoices(as_of).filter(sales_order=so))
        direct = None
    else:
        direct = Invoice.objects.select_related("customer").get(number=key, sales_order__isnull=True)
        invoices = list(_invoices(as_of).filter(pk=direct.pk))
    return _order_profit(key, so, direct, invoices, as_of, settings=HandlingSettings.load(), rates=_rates(),
                         billed=billed_to_date(key))


def orders(
    as_of: date, *, date_from: date | None = None, date_to: date | None = None, customer=None,
) -> list[OrderProfit]:
    """Every order with at least one invoice dated on or before as_of, filtered by order date and customer."""
    qs = _invoices(as_of)
    on_so, direct = Q(sales_order__isnull=False), Q(sales_order__isnull=True)
    if date_from is not None:
        qs = qs.filter((on_so & Q(sales_order__date__gte=date_from)) | (direct & Q(date__gte=date_from)))
    if date_to is not None:
        qs = qs.filter((on_so & Q(sales_order__date__lte=date_to)) | (direct & Q(date__lte=date_to)))
    if customer is not None:
        qs = qs.filter((on_so & Q(sales_order__customer=customer)) | (direct & Q(customer=customer)))

    groups: dict[str, list[Invoice]] = defaultdict(list)
    for inv in qs:
        groups[inv.sales_order.number if inv.sales_order_id else inv.number].append(inv)

    settings, rates, billed = HandlingSettings.load(), _rates(), _billed(groups)
    result = []
    for key, invoices in groups.items():
        so = invoices[0].sales_order
        result.append(_order_profit(key, so, None if so else invoices[0], invoices, as_of,
                                    settings=settings, rates=rates, billed=billed.get(key, ZERO)))
    result.sort(key=lambda o: (o.date, o.key))
    return result

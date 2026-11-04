"""FIFO cost lot tracking: which receipt each sale of one item drew on, traced by the same engine that costs it."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse

from ledger.balances import money

from .costing import Consume, Fill, Move, Short, replay, round_cents
from .models import CostingMethod, Item, StockMovement
from .reports import item_valuation

ZERO = Decimal("0.00")
NO_INVOICE = "No invoice yet"
SOLD_BEFORE = "Sold before received"
STILL_ESTIMATED = "Still estimated"


@dataclass
class Source:
    label: str
    url: str | None
    party: str


@dataclass
class Lot:
    movement: StockMovement
    date: date
    label: str
    url: str | None
    party: str
    quantity: int
    unit_cost: Decimal
    total: Decimal
    age_days: int = 0
    opening: bool = False
    on_hand_at_start: int = 0
    used_before_start: int = 0
    remaining_quantity: int = 0
    remaining_value: Decimal = ZERO


@dataclass
class LotRow:
    lot: Lot
    first: bool
    out_date: date | None = None
    out_label: str = ""
    out_url: str | None = None
    out_party: str = ""
    out_quantity: int | None = None
    tag: str = ""


@dataclass
class ShortRow:
    date: date
    label: str
    url: str | None
    party: str
    quantity: int
    estimate: Decimal
    resolved: str
    filled: bool


@dataclass
class LotReport:
    item: Item
    start: date
    end: date
    lots: list[Lot] = field(default_factory=list)
    rows: list[LotRow] = field(default_factory=list)
    shorts: list[ShortRow] = field(default_factory=list)
    received: int = 0
    sold: int = 0
    on_hand: int = 0
    value: Decimal = ZERO
    lots_quantity: int = 0
    lots_value: Decimal = ZERO
    difference: Decimal = ZERO


def _counted(item: Item, end: date):
    """Movements that count on `end`: dated by then and not voided by then (the valuation report's rule)."""
    return (StockMovement.objects.filter(item=item, date__lte=end)
            .exclude(Q(voided=True) & Q(voided_on__lte=end)).order_by("date", "id"))


def _to_move(m: StockMovement) -> Move:
    if m.quantity > 0:
        return Move(m.pk, m.date, m.quantity, m.unit_cost)
    if m.quantity < 0:
        return Move(m.pk, m.date, m.quantity)
    return Move(m.pk, m.date, 0, value_change=m.total_cost)


def _sources(movements: list[StockMovement]) -> dict[int, Source]:
    """Label, link, and counterparty for each movement, with one query per document type."""
    from purchasing.models import Bill, VendorCredit
    from sales.models import Invoice

    def numbers(source_type):
        return {m.source_id for m in movements if m.source_type == source_type and m.source_id}

    bills = {b.number: b.vendor.name for b in Bill.objects.filter(number__in=numbers("purchasing.bill")).select_related("vendor")}
    invoices = {i.number: i.customer.name
                for i in Invoice.objects.filter(number__in=numbers("sales.invoice")).select_related("customer")}
    credits = {c.number: c.vendor.name
               for c in VendorCredit.objects.filter(number__in=numbers("purchasing.vendor_credit")).select_related("vendor")}
    out = {}
    for m in movements:
        n = m.source_id
        if m.source_type == "purchasing.bill":
            out[m.pk] = Source(f"Bill {n}", reverse("purchasing:bill-detail", args=[n]), bills.get(n, ""))
        elif m.source_type == "sales.invoice":
            out[m.pk] = Source(f"Invoice {n}", reverse("sales:invoice-detail", args=[n]), invoices.get(n, ""))
        elif m.source_type == "purchasing.vendor_credit":
            out[m.pk] = Source(f"Vendor credit {n}", reverse("purchasing:credit-detail", args=[n]), credits.get(n, ""))
        elif m.source_type.startswith("migration."):
            out[m.pk] = Source("Zoho import", None, m.reason)
        else:
            out[m.pk] = Source(m.get_kind_display(), None, m.reason)
    return out


def fifo_lots(item: Item, start: date, end: date) -> LotReport:
    assert item.costing_method == CostingMethod.FIFO
    movements = list(_counted(item, end))
    by_id = {m.pk: m for m in movements}
    trace: list = []
    result = replay(CostingMethod.FIFO, [_to_move(m) for m in movements], item.purchase_cost, trace=trace)
    sources = _sources(movements)
    remaining = {l.source_id: l for l in result.layers}

    taken: dict[int, list] = {}  # lot id -> Consume and Fill events drawing on it
    fills_for: dict[int, list[Fill]] = {}  # outflow id -> fills
    for e in trace:
        if isinstance(e, Consume):
            taken.setdefault(e.layer_id, []).append(e)
        elif isinstance(e, Fill):
            taken.setdefault(e.inflow_id, []).append(e)
            fills_for.setdefault(e.outflow_id, []).append(e)

    report = LotReport(item, start, end)
    for m in movements:
        if m.quantity > 0 and start <= m.date <= end:
            report.received += m.quantity
        elif m.quantity < 0 and start <= m.date <= end:
            report.sold += -m.quantity

    for m in movements:
        if m.quantity <= 0:
            continue
        events = taken.get(m.pk, [])
        out_date = {id(e): by_id[e.outflow_id].date for e in events}
        before = sum(e.quantity for e in events if isinstance(e, Fill) or out_date[id(e)] < start)
        opening = m.date < start
        if opening and m.quantity - before <= 0:
            continue
        if m.date > end:
            continue
        src = sources[m.pk]
        layer = remaining.get(m.pk)
        left = layer.remaining_quantity if layer else 0
        if left == 0 and events:
            age_until = max([out_date[id(e)] for e in events if isinstance(e, Consume)] or [m.date])
        else:
            age_until = end
        lot = Lot(
            m, m.date, src.label, src.url, src.party, m.quantity, m.unit_cost, round_cents(m.quantity * m.unit_cost),
            age_days=max((age_until - m.date).days, 0), opening=opening,
            on_hand_at_start=m.quantity - before if opening else 0, used_before_start=before if opening else 0,
            remaining_quantity=left, remaining_value=layer.remaining_value if layer else ZERO)
        report.lots.append(lot)

        shown = [e for e in events if start <= out_date[id(e)] <= end or (isinstance(e, Fill) and not opening)]
        shown.sort(key=lambda e: (out_date[id(e)], e.outflow_id))
        if not shown:
            report.rows.append(LotRow(lot, True, tag=NO_INVOICE if left > 0 else ""))
            continue
        for n, e in enumerate(shown):
            out = sources[e.outflow_id]
            report.rows.append(LotRow(
                lot, n == 0, out_date[id(e)], out.label, out.url, out.party, e.quantity,
                tag=SOLD_BEFORE if isinstance(e, Fill) else ""))

    for e in trace:
        if not isinstance(e, Short):
            continue
        m = by_id[e.outflow_id]
        if not (start <= m.date <= end):
            continue
        fills = fills_for.get(e.outflow_id, [])
        filled = sum(f.quantity for f in fills)
        by = ", ".join(f"{sources[f.inflow_id].label} on {by_id[f.inflow_id].date.isoformat()}" for f in fills)
        if fills and filled >= e.quantity:
            resolved = f"Filled by {by}"
        elif fills:
            resolved = f"Partly filled by {by}; {e.quantity - filled} still estimated"
        else:
            resolved = STILL_ESTIMATED
        src = sources[m.pk]
        report.shorts.append(ShortRow(m.date, src.label, src.url, src.party, e.quantity, e.estimate, resolved,
                                      bool(fills) and filled >= e.quantity))

    report.on_hand, report.value = item_valuation(item, end)
    report.lots_quantity = result.quantity
    report.lots_value = money(result.value)
    report.difference = money(report.lots_value - report.value)
    return report

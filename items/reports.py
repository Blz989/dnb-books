"""Inventory reports. Valuation follows the ledger's dates so it can always be tied back to the balance sheet."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Count, Sum

from ledger.balances import account_totals, money, signed_balance
from ledger.models import Account, AccountSubtype

from .models import CostRevision, Item, StockMovement
from .stock import effective_total, stock_value

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ValuationRow:
    item: Item
    quantity: int
    value: Decimal


@dataclass(frozen=True)
class Valuation:
    as_of: date
    rows: list[ValuationRow]
    total: Decimal
    ledger_balance: Decimal
    difference: Decimal


@dataclass(frozen=True)
class MovementRow:
    movement: StockMovement
    effective_value: Decimal
    running_quantity: int


@dataclass(frozen=True)
class NegativeRow:
    item: Item
    quantity: int
    value: Decimal


def inventory_valuation(as_of: date) -> Valuation:
    quantity: dict[int, int] = defaultdict(int)
    value: dict[int, Decimal] = defaultdict(lambda: ZERO)

    # Movements dated on or before as_of, then the effect of voids and cost revisions dated by their own dates.
    for r in StockMovement.objects.filter(date__lte=as_of).values("item_id").annotate(q=Sum("quantity"), v=Sum("total_cost")):
        quantity[r["item_id"]] += r["q"]
        value[r["item_id"]] += money(r["v"])
    for r in StockMovement.objects.filter(voided=True, voided_on__lte=as_of).values("item_id").annotate(
            q=Sum("quantity"), v=Sum("void_amount")):
        quantity[r["item_id"]] -= r["q"]
        value[r["item_id"]] += money(r["v"])
    for r in CostRevision.objects.filter(date__lte=as_of).values("movement__item_id").annotate(v=Sum("delta")):
        value[r["movement__item_id"]] += money(r["v"])

    items = Item.objects.in_bulk(list(quantity))
    rows = [
        ValuationRow(items[pk], quantity[pk], money(value[pk]))
        for pk in quantity if quantity[pk] != 0 or money(value[pk]) != 0
    ]
    rows.sort(key=lambda r: r.item.sku.lower())
    total = money(sum((r.value for r in rows), ZERO))

    inventory_ids = list(Account.objects.filter(subtype=AccountSubtype.INVENTORY).values_list("pk", flat=True))
    totals = account_totals(start=None, end=as_of)
    accounts = Account.objects.in_bulk(inventory_ids)
    ledger_balance = money(sum(
        (signed_balance(accounts[pk].type, *totals.get(pk, (ZERO, ZERO))) for pk in inventory_ids), ZERO))
    return Valuation(as_of, rows, total, ledger_balance, money(total - ledger_balance))


def stock_movements(start: date, end: date, item: Item | None = None) -> list[MovementRow]:
    movements = StockMovement.objects.filter(date__gte=start, date__lte=end).select_related(
        "item", "journal_entry", "created_by")
    if item is not None:
        movements = movements.filter(item=item)
    movements = movements.order_by("item__sku", "date", "id")

    starting = {
        r["item_id"]: r["q"] or 0
        for r in StockMovement.objects.filter(date__lt=start, voided=False).values("item_id").annotate(q=Sum("quantity"))
    }
    running = dict(starting)
    rows = []
    for m in movements:
        if not m.voided:
            running[m.item_id] = running.get(m.item_id, 0) + m.quantity
        rows.append(MovementRow(m, effective_total(m), running.get(m.item_id, 0)))
    return rows


def negative_stock() -> list[NegativeRow]:
    negative = (
        StockMovement.objects.filter(voided=False).values("item_id").annotate(q=Sum("quantity")).filter(q__lt=0)
    )
    quantities = {r["item_id"]: r["q"] for r in negative}
    items = Item.objects.in_bulk(list(quantities))
    rows = [NegativeRow(items[pk], q, stock_value(items[pk])) for pk, q in quantities.items()]
    rows.sort(key=lambda r: r.item.sku.lower())
    return rows


def item_totals(item_ids=None) -> dict[int, tuple[int, Decimal]]:
    """Quantity and effective value (movement cost plus revisions) per item, live movements only."""
    movements = StockMovement.objects.filter(voided=False)
    revisions = CostRevision.objects.filter(movement__voided=False)
    if item_ids is not None:
        movements = movements.filter(item_id__in=item_ids)
        revisions = revisions.filter(movement__item_id__in=item_ids)
    totals: dict[int, tuple[int, Decimal]] = {}
    for r in movements.values("item_id").annotate(q=Sum("quantity"), v=Sum("total_cost")):
        totals[r["item_id"]] = (r["q"], money(r["v"]))
    for r in revisions.values("movement__item_id").annotate(v=Sum("delta")):
        q, v = totals.get(r["movement__item_id"], (0, ZERO))
        totals[r["movement__item_id"]] = (q, v + money(r["v"]))
    return totals


def negative_item_count() -> int:
    return (
        StockMovement.objects.filter(voided=False).values("item_id").annotate(q=Sum("quantity"))
        .filter(q__lt=0).count()
    )

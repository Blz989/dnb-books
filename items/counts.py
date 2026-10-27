"""Physical count workflow: snapshot, enter counts over several sessions, review, post."""
from dataclasses import dataclass
from datetime import date as date_type

from django.db import transaction
from django.utils import timezone

from .errors import CountError, CountNeedsConfirmation
from .models import (
    Category, CountScope, CountStatus, Item, ItemType, MovementKind, StockCount, StockCountLine, StockMovement,
)
from .stock import StockLine, on_hand, record_movements


@dataclass(frozen=True)
class CountDiff:
    line: StockCountLine
    current_on_hand: int
    difference: int
    moved_since_start: bool


def _require_open(count: StockCount) -> None:
    if count.status != CountStatus.OPEN:
        raise CountError(f"This count is already {CountStatus(count.status).label}.")


def start_count(*, name: str, category: Category | None, user) -> StockCount:
    with transaction.atomic():
        count = StockCount.objects.create(
            name=name, scope=CountScope.CATEGORY if category else CountScope.ALL, category=category,
            started_at=timezone.now(), started_by=user)
        items = Item.objects.filter(item_type=ItemType.STOCK, is_active=True)
        if category:
            items = items.filter(category=category)
        StockCountLine.objects.bulk_create([
            StockCountLine(count=count, item=item, expected_quantity=on_hand(item)) for item in items
        ])
        return count


def enter_counts(count: StockCount, counts: dict[int, int | None], user) -> None:
    count.refresh_from_db()
    _require_open(count)
    with transaction.atomic():
        lines = {l.item_id: l for l in count.lines.select_related("item")}
        for item_id, quantity in counts.items():
            line = lines.get(item_id)
            if line is None:
                continue
            if quantity is not None and quantity < 0:
                raise CountError(f"{line.item.sku}: a count can't be negative.")
            line.counted_quantity = quantity
            line.save(update_fields=["counted_quantity"])


def _moved_since_start(count: StockCount, item: Item) -> bool:
    return (
        StockMovement.objects.filter(item=item, voided=False, created_at__gt=count.started_at)
        .exclude(count_line__count=count).exists()
    )


def review(count: StockCount) -> list[CountDiff]:
    diffs = []
    for line in count.lines.select_related("item").filter(counted_quantity__isnull=False):
        current = on_hand(line.item)
        diffs.append(CountDiff(line, current, line.counted_quantity - current, _moved_since_start(count, line.item)))
    return diffs


def post_count(count: StockCount, *, date: date_type, user, confirm_moved: bool = False) -> list[StockMovement]:
    with transaction.atomic():
        count.refresh_from_db()
        _require_open(count)
        diffs = review(count)
        flagged = [d.line.item.sku for d in diffs if d.moved_since_start]
        if flagged and not confirm_moved:
            raise CountNeedsConfirmation(flagged)
        movements = []
        changed = [d for d in diffs if d.difference != 0]
        if changed:
            movements = record_movements(
                kind=MovementKind.COUNT, date=date, user=user, allow_negative=True,
                lines=[StockLine(d.line.item, d.difference, reason=f"Count: {count.name}", count_line=d.line)
                       for d in changed])
        count.status = CountStatus.POSTED
        count.posted_at = timezone.now()
        count.posted_by = user
        count.save()
        return movements


def cancel_count(count: StockCount, user) -> None:
    count.refresh_from_db()
    _require_open(count)
    count.status = CountStatus.CANCELED
    count.save()

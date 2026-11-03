"""The only code that changes stock. Posts through ledger.posting and turns cost differences into CostRevisions."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Max, Sum

from core.roles import require_admin
from ledger.balances import account_totals, money
from ledger.models import Account, AccountSubtype, JournalEntry
from ledger.periods import assert_open, is_closed
from ledger.posting import LineSpec, post_entry

from . import costing
from .costing import Move, replay, round_cents, unit_4dp
from .errors import ItemStateError, NegativeStockWarning, SettingsMissingError
from .models import (
    CostingMethod, CostRevision, FifoLayer, InventorySettings, Item, MovementKind, StockMovement,
)

ZERO = Decimal("0.00")
SENTINEL_ID = 10**12  # sorts after every real movement on the same date


@dataclass(frozen=True)
class StockLine:
    item: Item
    quantity: int
    reason: str = ""
    account: Account | None = None
    unit_cost: Decimal | None = None
    count_line: object | None = None  # StockCountLine, set by the count workflow


# ---- reads ----

def _live_movements(item: Item):
    return StockMovement.objects.filter(item=item, voided=False).order_by("date", "id")


def _moves(item: Item) -> list[Move]:
    moves = []
    for m in _live_movements(item):
        if m.quantity > 0:
            moves.append(Move(m.pk, m.date, m.quantity, m.unit_cost))
        elif m.quantity < 0:
            moves.append(Move(m.pk, m.date, m.quantity))
        else:
            moves.append(Move(m.pk, m.date, 0, value_change=m.total_cost))
    return moves


def _replay(item: Item, extra: Move | None = None) -> costing.ReplayResult:
    moves = _moves(item)
    if extra is not None:
        moves.append(extra)
        moves.sort(key=lambda m: (m.date, m.id))
    return replay(CostingMethod(item.costing_method), moves, item.purchase_cost)


def on_hand(item: Item) -> int:
    return _live_movements(item).aggregate(q=Sum("quantity"))["q"] or 0


def stock_value(item: Item) -> Decimal:
    return _replay(item).value


def current_unit_cost(item: Item) -> Decimal:
    return _replay(item).unit_cost


def effective_total(movement: StockMovement) -> Decimal:
    # SQLite sums in floating point; money() brings the total back to exact cents.
    return movement.total_cost + money(movement.revisions.aggregate(s=Sum("delta"))["s"])


# ---- writes ----

def _settings() -> InventorySettings:
    s = InventorySettings.load()
    if not s.is_configured:
        raise SettingsMissingError("Set up inventory accounts first.")
    return s


def _reserve_pk(model) -> int:
    """The next primary key, so a journal entry can carry its source id before the row exists.

    Safe because SQLite serializes writers (IMMEDIATE transactions) and AUTOINCREMENT never reuses ids.
    """
    with connection.cursor() as c:
        c.execute("SELECT seq FROM sqlite_sequence WHERE name = %s", [model._meta.db_table])
        row = c.fetchone()
    seq = row[0] if row else 0
    return max(seq, model.objects.aggregate(m=Max("pk"))["m"] or 0) + 1


def _post_value_change(*, delta: Decimal, inventory: Account, offset: Account, date: date_type, user,
                       memo: str, source_type: str, source_id: str) -> JournalEntry:
    """delta > 0 raises inventory (Dr inventory, Cr offset); delta < 0 lowers it."""
    amount = abs(delta)
    if delta > 0:
        lines = [LineSpec(inventory, debit=amount), LineSpec(offset, credit=amount)]
    else:
        lines = [LineSpec(offset, debit=amount), LineSpec(inventory, credit=amount)]
    return post_entry(date=date, user=user, memo=memo, source_type=source_type, source_id=source_id, lines=lines)


def _memo(prefix: str, sku: str, reason: str) -> str:
    return f"{prefix} {sku}: {reason}" if reason else f"{prefix} {sku}"


def _check_lines(lines: Sequence[StockLine], kind: MovementKind) -> None:
    for n, line in enumerate(lines, 1):
        item = line.item
        if not item.is_stock or not item.is_active:
            raise ItemStateError(f"Line {n}: {item.sku} is not an active stock item.")
        if line.quantity == 0:
            raise ItemStateError(f"Line {n}: enter a quantity other than zero.")
        if kind == MovementKind.OPENING and line.quantity < 0:
            raise ItemStateError(f"Line {n}: opening quantity must be above zero.")
        if line.unit_cost is not None and line.unit_cost.as_tuple().exponent < -4:
            raise ItemStateError(f"Line {n}: unit cost can have at most 4 decimal places.")


def _offset_account(kind: MovementKind, line: StockLine, s: InventorySettings, n: int) -> Account:
    if line.account is not None:
        account = line.account
    elif kind == MovementKind.OPENING:
        account = s.opening_equity_account
    elif kind in (MovementKind.ADJUSTMENT, MovementKind.COUNT):
        account = s.shrinkage_account
    else:
        raise ItemStateError(f"Line {n}: choose an account for this movement.")
    if account.subtype == AccountSubtype.INVENTORY:
        raise ItemStateError(f"Line {n}: choose an account other than an inventory account.")
    return account


def record_movements(
    *, kind: MovementKind, date: date_type, lines: Sequence[StockLine], user, allow_negative: bool = False,
    source_type: str = "items.movement", source_id: str = "",
) -> list[StockMovement]:
    with transaction.atomic():
        s = _settings()
        assert_open(date)
        _check_lines(lines, kind)

        running: dict[int, int] = {}
        warnings = []
        for n, line in enumerate(lines, 1):
            before = running.setdefault(line.item.pk, on_hand(line.item))
            after = before + line.quantity
            running[line.item.pk] = after
            if line.quantity < 0 and after < 0:
                warnings.append(f"Line {n}: {line.item.sku} has {before} on hand. Saving will take it to {after}. Continue?")
        if warnings and not allow_negative:
            raise NegativeStockWarning(warnings)

        movements, touched = [], {}
        for n, line in enumerate(lines, 1):
            item = line.item
            unit_cost = None
            if line.quantity > 0:
                unit_cost = line.unit_cost if line.unit_cost is not None else current_unit_cost(item)
            probe = Move(SENTINEL_ID, date, line.quantity, unit_cost)
            total = _replay(item, probe).costed[SENTINEL_ID].total_cost
            if line.quantity < 0:
                unit_cost = unit_4dp(abs(total) / abs(line.quantity))
            offset = _offset_account(kind, line, s, n)

            pk = _reserve_pk(StockMovement)
            entry = None
            if total != 0:
                entry = _post_value_change(
                    delta=total, inventory=item.inventory_account, offset=offset, date=date, user=user,
                    memo=_memo(MovementKind(kind).label, item.sku, line.reason),
                    source_type="items.movement", source_id=str(pk))
            movement = StockMovement.objects.create(
                pk=pk, item=item, date=date, kind=kind, quantity=line.quantity, unit_cost=unit_cost,
                total_cost=total, offset_account=offset, reason=line.reason, source_type=source_type,
                source_id=source_id, journal_entry=entry, count_line=line.count_line, created_by=user)
            movements.append(movement)
            touched[item.pk] = item
        for item in touched.values():
            replay_item(item, trigger_date=date, user=user)
        return movements


def record_opening_batch(*, date: date_type, lines: Sequence[StockLine], user) -> list[StockMovement]:
    """Opening stock for many items on one date, posted as one journal entry per inventory account.

    Used by the CSV import so a large file doesn't fill the journal with one entry per item.
    Each item may appear once.
    """
    with transaction.atomic():
        s = _settings()
        assert_open(date)
        _check_lines(lines, MovementKind.OPENING)
        if len({line.item.pk for line in lines}) != len(lines):
            raise ItemStateError("Each item can appear only once in an opening batch.")

        planned = []
        for n, line in enumerate(lines, 1):
            unit_cost = line.unit_cost if line.unit_cost is not None else current_unit_cost(line.item)
            total = _replay(line.item, Move(SENTINEL_ID, date, line.quantity, unit_cost)).costed[SENTINEL_ID].total_cost
            planned.append((line, unit_cost, total, _offset_account(MovementKind.OPENING, line, s, n)))

        entries: dict[tuple[int, int], JournalEntry] = {}
        groups: dict[tuple[int, int], Decimal] = {}
        for line, _, total, offset in planned:
            key = (line.item.inventory_account_id, offset.pk)
            groups[key] = groups.get(key, ZERO) + total
        for (inventory_id, offset_id), total in groups.items():
            if total != 0:
                entries[(inventory_id, offset_id)] = _post_value_change(
                    delta=total, inventory=Account.objects.get(pk=inventory_id), offset=Account.objects.get(pk=offset_id),
                    date=date, user=user, memo=f"Opening stock, {len(lines)} items", source_type="items.import",
                    source_id=date.isoformat())

        movements = []
        for line, unit_cost, total, offset in planned:
            movements.append(StockMovement.objects.create(
                item=line.item, date=date, kind=MovementKind.OPENING, quantity=line.quantity, unit_cost=unit_cost,
                total_cost=total, offset_account=offset, reason=line.reason, source_type="items.import",
                journal_entry=entries.get((line.item.inventory_account_id, offset.pk)) if total != 0 else None,
                created_by=user))
        for line in lines:
            replay_item(line.item, trigger_date=date, user=user)
        return movements


def record_value_adjustment(
    *, item: Item, date: date_type, amount: Decimal, reason: str, user, account: Account | None = None,
) -> StockMovement:
    require_admin(user)
    with transaction.atomic():
        s = _settings()
        assert_open(date)
        if not item.is_stock or not item.is_active:
            raise ItemStateError(f"{item.sku} is not an active stock item.")
        if amount == 0:
            raise ItemStateError("Enter an amount other than zero.")
        if amount.as_tuple().exponent < -2:
            raise ItemStateError("Amounts can have at most 2 decimal places.")
        if on_hand(item) <= 0:
            raise ItemStateError(f"{item.sku} has no stock to revalue.")
        _replay(item, Move(SENTINEL_ID, date, 0, value_change=amount))  # raises CostingError if it can't apply
        offset = account or s.writedown_account
        pk = _reserve_pk(StockMovement)
        entry = _post_value_change(
            delta=amount, inventory=item.inventory_account, offset=offset, date=date, user=user,
            memo=_memo(MovementKind.VALUE_ADJUSTMENT.label, item.sku, reason),
            source_type="items.movement", source_id=str(pk))
        movement = StockMovement.objects.create(
            pk=pk, item=item, date=date, kind=MovementKind.VALUE_ADJUSTMENT, quantity=0, unit_cost=0,
            total_cost=amount, offset_account=offset, reason=reason, source_type="items.movement",
            journal_entry=entry, created_by=user)
        replay_item(item, trigger_date=date, user=user)
        return movement


def void_movement(
    movement: StockMovement, *, user, reason: str, date: date_type | None = None, allow_negative: bool = False,
    system: bool = False,
) -> StockMovement:
    """Reverse a movement's full effective value, mark it voided, and recost the item's later outflows.

    system=True is for services that void on a user's behalf (editing an invoice). No view passes it.
    """
    if not system:
        require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise ItemStateError("A reason is required to void a movement.")
    with transaction.atomic():
        current = StockMovement.objects.select_related("item", "offset_account").get(pk=movement.pk)
        if current.voided:
            raise ItemStateError("This movement is already voided.")
        when = date or current.date
        if when < current.date:
            raise ItemStateError("The void date can't be before the movement date.")
        assert_open(when)
        item = current.item
        if current.quantity > 0 and not allow_negative:
            held = on_hand(item)
            if held - current.quantity < 0:
                raise NegativeStockWarning([
                    f"{item.sku} has {held} on hand. Voiding this movement will take it to {held - current.quantity}. Continue?"])
        effective = effective_total(current)
        entry = None
        if effective != 0:
            entry = _post_value_change(
                delta=-effective, inventory=item.inventory_account, offset=current.offset_account, date=when,
                user=user, memo=_memo(f"Void of {MovementKind(current.kind).label}", item.sku, reason),
                source_type="items.void", source_id=str(current.pk))
        current.voided = True
        current.voided_on = when
        current.void_amount = -effective
        current.void_entry = entry
        current.save()
        replay_item(item, trigger_date=when, user=user)
    movement.refresh_from_db()
    return movement


def replay_item(item: Item, *, trigger_date: date_type, user) -> list[CostRevision]:
    """Recompute every outflow's cost and post a CostRevision for each difference."""
    with transaction.atomic():
        result = _replay(item)
        revisions = []
        # One query for every movement's corrections so far (effective_total would query once per movement).
        corrections = dict(
            CostRevision.objects.filter(movement__item=item).values("movement_id")
            .annotate(s=Sum("delta")).values_list("movement_id", "s"))
        for m in _live_movements(item).filter(quantity__lt=0):
            difference = result.costed[m.pk].total_cost - (m.total_cost + money(corrections.get(m.pk)))
            if difference == 0:
                continue
            when = m.date if not is_closed(m.date) else trigger_date
            pk = _reserve_pk(CostRevision)
            entry = _post_value_change(
                delta=difference, inventory=item.inventory_account, offset=m.offset_account, date=when, user=user,
                memo=f"Cost correction {item.sku}", source_type="items.cost_revision", source_id=str(pk))
            revisions.append(CostRevision.objects.create(
                pk=pk, movement=m, date=when, delta=difference, journal_entry=entry))
        FifoLayer.objects.filter(item=item).delete()
        FifoLayer.objects.bulk_create([
            FifoLayer(item=item, source_movement_id=l.source_id, original_quantity=l.original_quantity,
                      remaining_quantity=l.remaining_quantity, remaining_value=l.remaining_value)
            for l in result.layers
        ])
        return revisions


def check_invariants() -> list[str]:
    """Empty when healthy. Spec 5.7: effective movement costs match the ledger; layers match quantity."""
    problems = []
    totals = account_totals(start=None, end=date_type.max)
    for account in Account.objects.filter(subtype=AccountSubtype.INVENTORY):
        stock = ZERO
        for item in Item.objects.filter(inventory_account=account):
            item_value = ZERO
            for m in StockMovement.objects.filter(item=item, voided=False):
                item_value += effective_total(m)
            stock += item_value
            if item.is_stock:
                expected = _replay(item).value
                if item_value != expected:
                    problems.append(f"{item.sku}: movements say {item_value:,.2f}, costing says {expected:,.2f}.")
                if item.costing_method == CostingMethod.FIFO:
                    layers = FifoLayer.objects.filter(item=item).aggregate(q=Sum("remaining_quantity"))["q"] or 0
                    if layers != max(on_hand(item), 0):
                        problems.append(f"{item.sku}: FIFO layers hold {layers}, stock says {on_hand(item)}.")
        debit, credit = totals.get(account.pk, (ZERO, ZERO))
        ledger = money(debit - credit)
        if money(stock) != ledger:
            problems.append(f"{account.number} {account.name}: stock says {stock:,.2f}, ledger says {ledger:,.2f}.")
    return problems

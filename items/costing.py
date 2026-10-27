"""Pure costing engine: replays an item's movements and returns each movement's correct cost.

No database access. Callers pass moves sorted by (date, id). Sign convention:
CostedMove.total_cost is the signed change in inventory value (outflows are negative).
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal

from .errors import CostingError
from .models import CostingMethod

CENT = Decimal("0.01")
FOUR_DP = Decimal("0.0001")
ZERO = Decimal("0.00")


def round_cents(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def unit_4dp(x: Decimal) -> Decimal:
    return x.quantize(FOUR_DP, rounding=ROUND_HALF_UP)


def _fmt_date(d: date_type) -> str:
    return f"{d:%b} {d.day}, {d.year}"


@dataclass(frozen=True)
class Move:
    id: int
    date: date_type
    quantity: int
    unit_cost: Decimal | None = None
    value_change: Decimal = Decimal("0")


@dataclass(frozen=True)
class CostedMove:
    id: int
    total_cost: Decimal
    estimated: bool


@dataclass(frozen=True)
class Layer:
    source_id: int
    original_quantity: int
    remaining_quantity: int
    remaining_value: Decimal


@dataclass(frozen=True)
class ReplayResult:
    costed: dict[int, CostedMove]
    quantity: int
    value: Decimal
    layers: tuple[Layer, ...]
    unit_cost: Decimal


class _Backlog:
    """Outflow units that were costed at an estimate because stock was short, oldest first."""

    def __init__(self):
        self.entries: list[list] = []  # [outflow id, short units left, estimate left]

    def add(self, move_id: int, units: int, estimate: Decimal) -> None:
        self.entries.append([move_id, units, estimate])

    @property
    def units(self) -> int:
        return sum(e[1] for e in self.entries)

    @property
    def estimate(self) -> Decimal:
        return sum((e[2] for e in self.entries), ZERO)


def replay(method: CostingMethod, moves: Sequence[Move], fallback_unit_cost: Decimal) -> ReplayResult:
    keys = [(m.date, m.id) for m in moves]
    assert keys == sorted(keys), "moves must be sorted by (date, id)"
    fifo = method == CostingMethod.FIFO

    totals: dict[int, Decimal] = {}
    estimated: dict[int, bool] = {}
    layers: list[list] = []  # [source id, original qty, remaining qty, remaining value]
    backlog = _Backlog()
    phys_qty = 0  # units physically on hand; never negative
    phys_value = ZERO
    average = Decimal("0")
    last_unit_cost = fallback_unit_cost

    for m in moves:
        if m.quantity > 0:
            if m.unit_cost is None:
                raise ValueError(f"Inflow {m.id} needs a unit cost.")
            inflow_value = round_cents(m.quantity * m.unit_cost)
            totals[m.id] = inflow_value
            estimated[m.id] = False

            # Fill the shortfall first, oldest outflow first, at this inflow's cost.
            remaining = m.quantity
            fills: list[tuple[list, int, Decimal]] = []
            for entry in backlog.entries:
                if remaining == 0:
                    break
                k = min(remaining, entry[1])
                fills.append((entry, k, round_cents(k * m.unit_cost)))
                remaining -= k
            if fills and remaining == 0:
                # Whole inflow went to the backlog: make the fills add up to the inflow value exactly.
                last = fills[-1]
                fills[-1] = (last[0], last[1], inflow_value - sum((f[2] for f in fills[:-1]), ZERO))
            fill_cost = ZERO
            for entry, k, cost in fills:
                released = entry[2] if k == entry[1] else round_cents(entry[2] * k / entry[1])
                totals[entry[0]] += released - cost
                entry[1] -= k
                entry[2] -= released
                fill_cost += cost
                if entry[1] == 0:
                    estimated[entry[0]] = False
            backlog.entries = [e for e in backlog.entries if e[1] > 0]

            if remaining > 0:
                add_value = inflow_value - fill_cost
                if fifo:
                    layers.append([m.id, remaining, remaining, add_value])
                else:
                    phys_value += add_value
                    phys_qty += remaining
                    average = unit_4dp(phys_value / phys_qty)
                phys_qty = sum(l[2] for l in layers) if fifo else phys_qty
                phys_value = sum((l[3] for l in layers), ZERO) if fifo else phys_value
                last_unit_cost = m.unit_cost if fifo else average
            else:
                last_unit_cost = m.unit_cost

        elif m.quantity < 0:
            want = -m.quantity
            take = min(want, phys_qty)
            short = want - take
            cost = ZERO
            if take:
                if fifo:
                    left = take
                    while left:
                        layer = layers[0]
                        k = min(left, layer[2])
                        part = layer[3] if k == layer[2] else round_cents(layer[3] * k / layer[2])
                        layer[2] -= k
                        layer[3] -= part
                        cost += part
                        left -= k
                        if layer[2] == 0:
                            layers.pop(0)
                    phys_qty = sum(l[2] for l in layers)
                    phys_value = sum((l[3] for l in layers), ZERO)
                else:
                    cost = phys_value if take == phys_qty else round_cents(average * take)
                    phys_qty -= take
                    phys_value -= cost
            estimate = ZERO
            if short:
                estimate = round_cents(last_unit_cost * short)
                backlog.add(m.id, short, estimate)
            totals[m.id] = -(cost + estimate)
            estimated[m.id] = bool(short)

        else:
            if phys_qty <= 0:
                raise CostingError(f"The value adjustment on {_fmt_date(m.date)} has no stock to apply to.")
            if phys_value + m.value_change < 0:
                raise CostingError(f"The value adjustment on {_fmt_date(m.date)} would make stock value negative.")
            totals[m.id] = m.value_change
            estimated[m.id] = False
            if fifo:
                remaining_change = m.value_change
                for i, layer in enumerate(layers):
                    if i == len(layers) - 1:
                        share = remaining_change
                    else:
                        share = round_cents(m.value_change * layer[2] / phys_qty)
                    layer[3] += share
                    remaining_change -= share
                phys_value = sum((l[3] for l in layers), ZERO)
            else:
                phys_value += m.value_change
                average = unit_4dp(phys_value / phys_qty)
                last_unit_cost = average

    quantity = phys_qty - backlog.units
    value = phys_value - backlog.estimate
    if fifo and layers:
        last_unit_cost = layers[-1][3] / layers[-1][2]  # newest layer, so value adjustments show up
    result_layers = tuple(Layer(l[0], l[1], l[2], l[3]) for l in layers) if fifo else ()
    return ReplayResult(
        costed={m.id: CostedMove(m.id, totals[m.id], estimated[m.id]) for m in moves},
        quantity=quantity,
        value=value,
        layers=result_layers,
        unit_cost=unit_4dp(last_unit_cost),
    )

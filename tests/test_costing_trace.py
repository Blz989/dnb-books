from collections import defaultdict
from datetime import date
from decimal import Decimal

from hypothesis import given, settings

from items.costing import Consume, Fill, Move, Short, replay
from items.errors import CostingError
from items.models import CostingMethod

from .test_costing import move_lists

D = Decimal
FIFO = CostingMethod.FIFO


def d(day):
    return date(2026, 10, day)


@given(moves=move_lists())
@settings(max_examples=200, deadline=None)
def test_trace_changes_nothing_and_accounts_for_every_unit(moves):
    try:
        plain = replay(FIFO, moves, D("1.00"))
    except CostingError:
        return
    trace = []
    traced = replay(FIFO, moves, D("1.00"), trace=trace)
    assert traced == plain
    taken = defaultdict(int)
    shorts = defaultdict(int)
    filled = defaultdict(int)
    per_layer = defaultdict(int)
    for e in trace:
        if isinstance(e, Consume):
            taken[e.outflow_id] += e.quantity
            per_layer[e.layer_id] += e.quantity
        elif isinstance(e, Short):
            shorts[e.outflow_id] += e.quantity
        else:
            filled[e.outflow_id] += e.quantity
    for m in moves:
        if m.quantity < 0:
            assert taken[m.id] + shorts[m.id] == -m.quantity
            assert filled[m.id] <= shorts[m.id]
        if m.quantity > 0:
            assert per_layer[m.id] <= m.quantity
    remaining = {l.source_id: l.remaining_quantity for l in traced.layers}
    for layer_id, qty in remaining.items():
        fills_from = sum(e.quantity for e in trace if isinstance(e, Fill) and e.inflow_id == layer_id)
        original = next(m.quantity for m in moves if m.id == layer_id)
        assert per_layer[layer_id] + fills_from + qty == original


def test_trace_pairs_layers_and_fills():
    moves = [Move(1, d(1), -3), Move(2, d(2), 5, D("2.00")), Move(3, d(3), 4, D("3.00")), Move(4, d(4), -4)]
    trace = []
    replay(FIFO, moves, D("1.00"), trace=trace)
    assert trace == [
        Short(1, 3, D("3.00")),
        Fill(2, 1, 3, D("6.00")),
        Consume(2, 4, 2, D("4.00")),
        Consume(3, 4, 2, D("6.00")),
    ]


def test_weighted_average_appends_nothing():
    trace = []
    replay(CostingMethod.WEIGHTED_AVERAGE, [Move(1, d(1), 5, D("2.00")), Move(2, d(2), -7)], D("1.00"), trace=trace)
    assert trace == []

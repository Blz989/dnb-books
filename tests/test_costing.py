from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from items.costing import Move, replay
from items.errors import CostingError
from items.models import CostingMethod

D = Decimal
FIFO = CostingMethod.FIFO
WAVG = CostingMethod.WEIGHTED_AVERAGE


def d(day):
    return date(2026, 10, day)


def test_example_a_fifo():
    r = replay(FIFO, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 10, D("1.50")), Move(3, d(3), -15)], D("0"))
    assert r.costed[3].total_cost == D("-17.50") and (r.quantity, r.value) == (5, D("7.50"))
    assert r.unit_cost == D("1.5000")
    assert [(l.source_id, l.remaining_quantity, l.remaining_value) for l in r.layers] == [(2, 5, D("7.50"))]


def test_example_b_weighted_average():
    r = replay(WAVG, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 10, D("1.50")), Move(3, d(3), -15)], D("0"))
    assert r.costed[3].total_cost == D("-18.75") and r.value == D("6.25") and r.unit_cost == D("1.2500")


def test_example_c_last_outflow_clears_value():
    r = replay(WAVG, [Move(1, d(1), 3, D("0.3333333333")), Move(2, d(2), -1), Move(3, d(3), -1), Move(4, d(4), -1)], D("0"))
    # inflow value rounds to 1.00
    assert [r.costed[i].total_cost for i in (2, 3, 4)] == [D("-0.33"), D("-0.33"), D("-0.34")] and r.value == D("0.00")


def test_example_d_negative_then_fill():
    before = replay(FIFO, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5)], D("0"))
    assert before.costed[2].total_cost == D("-5.00") and before.costed[2].estimated
    after = replay(FIFO, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5), Move(3, d(3), 10, D("1.20"))], D("0"))
    assert after.costed[2].total_cost == D("-5.60") and not after.costed[2].estimated
    assert (after.quantity, after.value) == (7, D("8.40"))


def test_example_d_weighted_average():
    r = replay(WAVG, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5), Move(3, d(3), 10, D("1.20"))], D("0"))
    assert r.costed[2].total_cost == D("-5.60") and (r.quantity, r.value) == (7, D("8.40"))
    assert r.unit_cost == D("1.2000")


def test_example_e_backdated_inflow():
    r = replay(FIFO, [Move(9, date(2026, 9, 25), 10, D("0.80")), Move(1, date(2026, 10, 1), 10, D("1.00")),
                      Move(2, date(2026, 10, 10), -5)], D("0"))
    assert r.costed[2].total_cost == D("-4.00") and r.value == D("14.00")


def test_negative_stock_value_is_the_estimate_until_filled():
    r = replay(FIFO, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), -5)], D("0"))
    assert (r.quantity, r.value) == (-3, D("-3.00"))


def test_partial_fill_keeps_outflow_estimated():
    r = replay(FIFO, [Move(1, d(1), -5), Move(2, d(2), 2, D("2.00"))], D("1.00"))
    assert r.costed[1].estimated and r.costed[1].total_cost == D("-4.00") + D("-3.00")  # 2 @2.00 + 3 @1.00 estimate
    assert (r.quantity, r.value) == (-3, D("-3.00"))


def test_inflow_that_exactly_fills_backlog_leaves_no_stranded_cent():
    r = replay(WAVG, [Move(1, d(1), -3), Move(2, d(2), 3, D("0.3333"))], D("0.50"))
    assert r.quantity == 0 and r.value == D("0.00") and r.costed[1].total_cost == D("-1.00")


def test_value_adjustment_without_stock_raises():
    with pytest.raises(CostingError, match="The value adjustment on Oct 25, 2026 has no stock to apply to."):
        replay(FIFO, [Move(1, d(25), 0, value_change=D("-2.00"))], D("1.00"))


def test_value_adjustment_cannot_make_value_negative():
    with pytest.raises(CostingError, match="would make stock value negative"):
        replay(WAVG, [Move(1, d(1), 2, D("1.00")), Move(2, d(2), 0, value_change=D("-5.00"))], D("1.00"))


def test_fifo_value_adjustment_spreads_across_layers():
    r = replay(FIFO, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 10, D("1.50")),
                      Move(3, d(3), 0, value_change=D("-5.00"))], D("0"))
    # Spread by value share (10.00 and 15.00 of 25.00), not by quantity, so no layer can go below zero.
    assert [l.remaining_value for l in r.layers] == [D("8.00"), D("12.00")] and r.value == D("20.00")


def test_wavg_value_adjustment_changes_average():
    r = replay(WAVG, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 0, value_change=D("2.00")), Move(3, d(3), -5)], D("0"))
    assert r.costed[3].total_cost == D("-6.00") and r.value == D("6.00")


def test_never_had_stock_uses_fallback():
    r = replay(FIFO, [Move(1, d(1), -2)], D("3.25"))
    assert r.costed[1].total_cost == D("-6.50") and r.costed[1].estimated


def test_unsorted_moves_rejected():
    with pytest.raises(AssertionError):
        replay(FIFO, [Move(2, d(2), 1, D("1.00")), Move(1, d(1), 1, D("1.00"))], D("0"))


@st.composite
def move_lists(draw):
    n = draw(st.integers(1, 14))
    moves = []
    for i in range(1, n + 1):
        kind = draw(st.sampled_from(["in", "in", "out", "out", "adj"]))
        day = draw(st.integers(1, 28))
        if kind == "in":
            cost = D(draw(st.integers(1, 99999))) / D(10000)
            moves.append(Move(i, d(day), draw(st.integers(1, 20)), cost.quantize(D("0.0001"))))
        elif kind == "out":
            moves.append(Move(i, d(day), -draw(st.integers(1, 25))))
        else:
            moves.append(Move(i, d(day), 0, value_change=D(draw(st.integers(-500, 500))) / D(100) or D("0.01")))
    return sorted(moves, key=lambda m: (m.date, m.id))


@pytest.mark.parametrize("method", [FIFO, WAVG])
@given(moves=move_lists(), fallback=st.integers(0, 5000).map(lambda n: D(n) / D(1000)))
@settings(max_examples=200, deadline=None)
def test_value_equals_inflows_plus_outflows_plus_adjustments(method, moves, fallback):
    try:
        r = replay(method, moves, fallback)
    except CostingError:
        return
    inflow_values = sum((m.unit_cost * m.quantity for m in moves if m.quantity > 0), D("0"))
    # inflow value is rounded to the cent per movement
    inflow_values = sum(
        ((m.unit_cost * m.quantity).quantize(D("0.01"), rounding="ROUND_HALF_UP") for m in moves if m.quantity > 0), D("0"))
    outflows = sum((r.costed[m.id].total_cost for m in moves if m.quantity < 0), D("0"))
    adjustments = sum((m.value_change for m in moves if m.quantity == 0), D("0"))
    assert r.value == inflow_values + outflows + adjustments
    assert r.quantity == sum(m.quantity for m in moves)
    if r.quantity > 0:
        assert sum(l.remaining_quantity for l in r.layers) == r.quantity if method == FIFO else True


def test_fifo_unit_cost_follows_newest_layer_after_value_adjustment():
    r = replay(FIFO, [Move(1, d(1), 10, D("1.00")), Move(2, d(2), 0, value_change=D("5.00"))], D("0"))
    assert r.unit_cost == D("1.5000")

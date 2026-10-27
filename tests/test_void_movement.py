from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from items.errors import ItemStateError
from items.models import CostingMethod, CostRevision, MovementKind, StockMovement
from items.stock import StockLine, check_invariants, on_hand, record_movements, stock_value, void_movement
from ledger.errors import PeriodClosedError
from ledger.models import EntryStatus
from ledger.periods import close_period

D = Decimal
ADJ = MovementKind.ADJUSTMENT


def d(month, day):
    return date(2026, month, day)


def test_void_inflow_recosts_later_outflow(inv_settings, admin_user, make_item):
    item = make_item("V-1")
    first, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJ, date=d(10, 2), lines=[StockLine(item, 10, unit_cost=D("2.00"))], user=admin_user)
    out, = record_movements(kind=ADJ, date=d(10, 3), lines=[StockLine(item, -10)], user=admin_user)
    assert out.total_cost == D("-10.00")
    void_movement(first, user=admin_user, reason="Wrong item", date=d(10, 4))
    rev = CostRevision.objects.get()
    assert rev.delta == D("-10.00") and rev.movement == out and rev.date == d(10, 3)
    first.refresh_from_db()
    assert first.voided and first.voided_on == d(10, 4) and first.void_amount == D("-10.00")
    assert first.void_entry.source_type == "items.void" and first.void_entry.status == EntryStatus.POSTED
    assert first.void_entry.memo == "Void of Quantity adjustment V-1: Wrong item"
    assert (on_hand(item), stock_value(item)) == (0, D("0.00"))
    assert check_invariants() == []


def test_void_negative_estimate_inflow(inv_settings, admin_user, make_item):
    item = make_item("V-2")
    record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 2, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJ, date=d(10, 2), lines=[StockLine(item, -5)], user=admin_user, allow_negative=True)
    fill, = record_movements(kind=ADJ, date=d(10, 20), lines=[StockLine(item, 10, unit_cost=D("1.20"))], user=admin_user)
    assert [r.delta for r in CostRevision.objects.all()] == [D("-0.60")]
    void_movement(fill, user=admin_user, reason="Duplicate receipt", date=d(10, 21))
    assert [r.delta for r in CostRevision.objects.order_by("id")] == [D("-0.60"), D("0.60")]
    assert on_hand(item) == -3 and stock_value(item) == D("-3.00")
    assert check_invariants() == []


def test_void_zero_cost_movement_posts_no_entry(inv_settings, admin_user, make_item):
    item = make_item("V-3", purchase_cost="0")
    m, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 3)], user=admin_user)
    void_movement(m, user=admin_user, reason="Mistake")
    m.refresh_from_db()
    assert m.voided and m.void_entry is None and m.void_amount == D("0.00") and on_hand(item) == 0


def test_void_requires_reason_and_admin(inv_settings, admin_user, standard_user, make_item):
    item = make_item("V-4")
    m, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 3, unit_cost=D("1.00"))], user=admin_user)
    with pytest.raises(PermissionDenied):
        void_movement(m, user=standard_user, reason="x")
    with pytest.raises(ItemStateError, match="A reason is required to void a movement."):
        void_movement(m, user=admin_user, reason="  ")
    assert not StockMovement.objects.get(pk=m.pk).voided


def test_cannot_void_twice(inv_settings, admin_user, make_item):
    item = make_item("V-5")
    m, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 3, unit_cost=D("1.00"))], user=admin_user)
    void_movement(m, user=admin_user, reason="x")
    with pytest.raises(ItemStateError, match="This movement is already voided."):
        void_movement(m, user=admin_user, reason="again")


def test_void_in_closed_month_needs_open_date(inv_settings, admin_user, make_item):
    item = make_item("V-6")
    m, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 3, unit_cost=D("1.00"))], user=admin_user)
    close_period(2026, 10, admin_user)
    with pytest.raises(PeriodClosedError):
        void_movement(m, user=admin_user, reason="x")
    assert not StockMovement.objects.get(pk=m.pk).voided
    void_movement(m, user=admin_user, reason="x", date=d(11, 2))
    assert StockMovement.objects.get(pk=m.pk).voided_on == d(11, 2) and check_invariants() == []


def test_void_that_breaks_a_value_adjustment_rolls_back(inv_settings, admin_user, make_item):
    from items.errors import CostingError
    from items.stock import record_value_adjustment

    item = make_item("V-7")
    m, = record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_value_adjustment(item=item, date=d(10, 2), amount=D("-2.00"), reason="x", user=admin_user)
    with pytest.raises(CostingError):
        void_movement(m, user=admin_user, reason="x", date=d(10, 3))
    assert not StockMovement.objects.get(pk=m.pk).voided and check_invariants() == []


def test_void_date_cannot_precede_the_movement(inv_settings, admin_user, make_item):
    item = make_item("V-8")
    m, = record_movements(kind=ADJ, date=d(10, 10), lines=[StockLine(item, 3, unit_cost=D("1.00"))], user=admin_user)
    with pytest.raises(ItemStateError, match="The void date can't be before the movement date."):
        void_movement(m, user=admin_user, reason="x", date=d(10, 9))
    assert not StockMovement.objects.get(pk=m.pk).voided

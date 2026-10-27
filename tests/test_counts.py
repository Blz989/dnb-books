from datetime import date
from decimal import Decimal

import pytest

from items.counts import cancel_count, enter_counts, post_count, review, start_count
from items.errors import CountError, CountNeedsConfirmation
from items.models import Category, CountStatus, MovementKind, StockMovement
from items.stock import StockLine, check_invariants, on_hand, record_movements

D = Decimal
D1 = date(2026, 10, 1)
D2 = date(2026, 10, 20)


@pytest.fixture
def stocked(inv_settings, admin_user, make_item):
    paint = Category.objects.create(name="Paint")
    a = make_item("A-1", category=paint)
    b = make_item("B-1")
    c = make_item("C-1", is_active=False)
    for item, qty in ((a, 10), (b, 20)):
        record_movements(kind=MovementKind.OPENING, date=D1, lines=[StockLine(item, qty, unit_cost=D("2.00"))], user=admin_user)
    return a, b, c, paint


def line(count, item):
    return count.lines.get(item=item)


def test_start_snapshots_expected_quantities_for_active_stock_items(stocked, admin_user):
    a, b, c, _ = stocked
    count = start_count(name="Oct count", category=None, user=admin_user)
    assert {l.item.sku: l.expected_quantity for l in count.lines.select_related("item")} == {"A-1": 10, "B-1": 20}
    assert count.status == CountStatus.OPEN and count.started_by == admin_user


def test_start_can_be_limited_to_a_category(stocked, admin_user):
    a, b, c, paint = stocked
    count = start_count(name="Paint", category=paint, user=admin_user)
    assert [l.item.sku for l in count.lines.select_related("item")] == ["A-1"]
    assert count.scope == "CATEGORY" and count.category == paint


def test_partial_entry_across_two_calls_and_clearing(stocked, admin_user):
    a, b, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8}, admin_user)
    enter_counts(count, {b.pk: 21}, admin_user)
    assert (line(count, a).counted_quantity, line(count, b).counted_quantity) == (8, 21)
    enter_counts(count, {a.pk: None}, admin_user)
    assert line(count, a).counted_quantity is None and line(count, b).counted_quantity == 21


def test_negative_count_rejected(stocked, admin_user):
    a, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    with pytest.raises(CountError, match="A-1: a count can't be negative."):
        enter_counts(count, {a.pk: -1}, admin_user)
    assert line(count, a).counted_quantity is None


def test_review_lists_only_counted_lines_with_differences(stocked, admin_user):
    a, b, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8}, admin_user)
    (diff,) = review(count)
    assert (diff.line.item, diff.current_on_hand, diff.difference, diff.moved_since_start) == (a, 10, -2, False)


def test_post_creates_count_movements_with_shrinkage_entries(stocked, admin_user):
    a, b, *_ = stocked
    count = start_count(name="Oct count", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8, b.pk: 20}, admin_user)  # b matches: no movement
    movements = post_count(count, date=D2, user=admin_user)
    assert len(movements) == 1
    m = movements[0]
    assert (m.kind, m.quantity, m.total_cost, m.reason) == (MovementKind.COUNT, -2, D("-4.00"), "Count: Oct count")
    assert m.offset_account.number == "6500" and m.count_line == line(count, a)
    count.refresh_from_db()
    assert count.status == CountStatus.POSTED and count.posted_by == admin_user and count.posted_at
    assert on_hand(a) == 8 and check_invariants() == []


def test_uncounted_lines_are_ignored(stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    assert post_count(count, date=D2, user=admin_user) == []
    assert StockMovement.objects.filter(kind=MovementKind.COUNT).count() == 0


def test_items_that_moved_since_start_need_confirmation(stocked, admin_user):
    a, b, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    record_movements(kind=MovementKind.ADJUSTMENT, date=D2, lines=[StockLine(a, -1)], user=admin_user)
    enter_counts(count, {a.pk: 8, b.pk: 19}, admin_user)
    diffs = {d.line.item.sku: d for d in review(count)}
    assert diffs["A-1"].moved_since_start and diffs["A-1"].current_on_hand == 9 and diffs["A-1"].difference == -1
    assert not diffs["B-1"].moved_since_start
    with pytest.raises(CountNeedsConfirmation) as e:
        post_count(count, date=D2, user=admin_user)
    assert e.value.skus == ["A-1"]
    assert StockMovement.objects.filter(kind=MovementKind.COUNT).count() == 0
    post_count(count, date=D2, user=admin_user, confirm_moved=True)
    assert on_hand(a) == 8 and on_hand(b) == 19 and check_invariants() == []


def test_posting_twice_raises(stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    post_count(count, date=D2, user=admin_user)
    with pytest.raises(CountError, match="This count is already posted."):
        post_count(count, date=D2, user=admin_user)
    with pytest.raises(CountError, match="This count is already posted."):
        enter_counts(count, {}, admin_user)


def test_cancel(stocked, admin_user):
    a, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    cancel_count(count, admin_user)
    count.refresh_from_db()
    assert count.status == CountStatus.CANCELED
    with pytest.raises(CountError, match="This count is already canceled."):
        post_count(count, date=D2, user=admin_user)


def test_count_above_expected_posts_an_inflow_at_current_cost(stocked, admin_user):
    a, *_ = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 12}, admin_user)
    (m,) = post_count(count, date=D2, user=admin_user)
    assert (m.quantity, m.unit_cost, m.total_cost) == (2, D("2.0000"), D("4.00"))
    assert check_invariants() == []

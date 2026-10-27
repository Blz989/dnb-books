from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from items.counts import enter_counts, post_count, start_count
from items.models import Category, CountStatus, MovementKind, StockCount, StockMovement
from items.stock import StockLine, check_invariants, on_hand, record_movements

D = Decimal


@pytest.fixture
def stocked(inv_settings, admin_user, make_item):
    paint = Category.objects.create(name="Paint")
    a = make_item("A-1", category=paint, name="Alpha")
    b = make_item("B-1", name="Bravo")
    record_movements(kind=MovementKind.OPENING, date=date(2026, 10, 1), user=admin_user, lines=[
        StockLine(a, 10, unit_cost=D("2.00")), StockLine(b, 20, unit_cost=D("2.00"))])
    return a, b


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


def test_counts_require_login(client):
    assert client.get("/items/counts/").status_code == 302


def test_list_start_and_enter_flow(std, stocked, standard_user):
    assert b"No counts yet." in std.get("/items/counts/").content
    assert b"Count name" in std.get("/items/counts/new/").content
    r = std.post("/items/counts/new/", {"name": "Oct count", "scope": "ALL", "category": ""})
    count = StockCount.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/items/counts/{count.pk}/"
    assert count.lines.count() == 2 and count.started_by == standard_user
    assert b"Oct count" in std.get("/items/counts/").content


def test_start_limited_to_category(std, stocked):
    paint = Category.objects.get(name="Paint")
    std.post("/items/counts/new/", {"name": "Paint only", "scope": "CATEGORY", "category": paint.pk})
    assert [l.item.sku for l in StockCount.objects.get().lines.select_related("item")] == ["A-1"]


def test_start_requires_name_and_category_for_category_scope(std, stocked):
    r = std.post("/items/counts/new/", {"name": "", "scope": "ALL", "category": ""})
    assert r.status_code == 200 and b"Enter a name for this count." in r.content
    r = std.post("/items/counts/new/", {"name": "x", "scope": "CATEGORY", "category": ""})
    assert r.status_code == 200 and b"Choose a category." in r.content
    assert StockCount.objects.count() == 0


def test_enter_screen_is_blind(std, stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    html = std.get(f"/items/counts/{count.pk}/").content.decode()
    assert "A-1" in html and "Alpha" in html and "Expected" not in html


def test_enter_saves_and_filters(std, stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    a, b = stocked
    r = std.post(f"/items/counts/{count.pk}/", {f"count-{a.pk}": "8", f"count-{b.pk}": ""})
    assert r.status_code == 302
    lines = {l.item.sku: l.counted_quantity for l in count.lines.select_related("item")}
    assert lines == {"A-1": 8, "B-1": None}
    html = std.get(f"/items/counts/{count.pk}/?q=bravo").content.decode()
    assert "B-1" in html and "A-1" not in html


def test_page_two_keeps_page_one_values(std, inv_settings, admin_user, make_item):
    items = [make_item(f"P-{i:03d}") for i in range(55)]
    for item in items:
        record_movements(kind=MovementKind.OPENING, date=date(2026, 10, 1), user=admin_user,
                         lines=[StockLine(item, 5, unit_cost=D("1.00"))])
    count = start_count(name="Big", category=None, user=admin_user)
    std.post(f"/items/counts/{count.pk}/?page=1", {f"count-{items[0].pk}": "4"})
    r2 = std.get(f"/items/counts/{count.pk}/?page=2")
    assert b"P-050" in r2.content and b"P-049" not in r2.content
    std.post(f"/items/counts/{count.pk}/?page=2", {f"count-{items[54].pk}": "3"})
    counted = {l.item.sku: l.counted_quantity for l in count.lines.select_related("item") if l.counted_quantity is not None}
    assert counted == {"P-000": 4, "P-054": 3}


def test_bad_counts_show_messages_and_save_nothing(std, stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    a, b = stocked
    r = std.post(f"/items/counts/{count.pk}/", {f"count-{a.pk}": "-1"})
    assert r.status_code == 200 and b"A-1: a count can&#x27;t be negative." in r.content
    r = std.post(f"/items/counts/{count.pk}/", {f"count-{a.pk}": "2.5"})
    assert r.status_code == 200 and b"A-1: quantity must be a whole number." in r.content
    assert count.lines.filter(counted_quantity__isnull=False).count() == 0
    assert 'value="2.5"' in r.content.decode()  # what was typed stays in the box


def test_review_shows_differences_and_flags_moved_items(std, stocked, admin_user):
    a, b = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 20), lines=[StockLine(a, -1)], user=admin_user)
    enter_counts(count, {a.pk: 8, b.pk: 19}, admin_user)
    html = std.get(f"/items/counts/{count.pk}/review/").content.decode()
    assert "A-1" in html and "B-1" in html and "Moved since count started" in html
    assert "Expected" in html and "I&#x27;ve checked the flagged items" in html


def test_posting_flagged_without_checkbox_lists_skus(std, stocked, admin_user):
    a, b = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 20), lines=[StockLine(a, -1)], user=admin_user)
    enter_counts(count, {a.pk: 8}, admin_user)
    r = std.post(f"/items/counts/{count.pk}/review/", {"date": "2026-10-25"})
    assert r.status_code == 200 and b"A-1" in r.content and b"moved since the count started" in r.content
    assert StockMovement.objects.filter(kind=MovementKind.COUNT).count() == 0
    r = std.post(f"/items/counts/{count.pk}/review/", {"date": "2026-10-25", "confirm_moved": "on"})
    assert r.status_code == 302 and on_hand(a) == 8
    assert check_invariants() == []


def test_post_creates_movements_and_marks_posted(std, stocked, admin_user):
    a, b = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8, b.pk: 20}, admin_user)
    r = std.post(f"/items/counts/{count.pk}/review/", {"date": "2026-10-25"}, follow=True)
    assert b"Posted Oct: 1 adjustment." in r.content
    count.refresh_from_db()
    assert count.status == CountStatus.POSTED and on_hand(a) == 8


def test_posted_count_is_read_only(std, stocked, admin_user):
    a, b = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8}, admin_user)
    post_count(count, date=date(2026, 10, 25), user=admin_user)
    review = std.get(f"/items/counts/{count.pk}/review/").content.decode()
    assert "Posted" in review and "Post count" not in review
    r = std.get(f"/items/counts/{count.pk}/", follow=True)
    assert b"This count is already posted." in r.content
    r = std.post(f"/items/counts/{count.pk}/", {f"count-{a.pk}": "1"}, follow=True)
    assert b"This count is already posted." in r.content and count.lines.get(item=a).counted_quantity == 8


def test_post_errors_show_inline(std, stocked, admin_user):
    from ledger.periods import close_period

    a, b = stocked
    count = start_count(name="Oct", category=None, user=admin_user)
    enter_counts(count, {a.pk: 8}, admin_user)
    close_period(2026, 10, admin_user)
    r = std.post(f"/items/counts/{count.pk}/review/", {"date": "2026-10-25"})
    assert r.status_code == 200 and b"October 2026 is closed." in r.content
    r = std.post(f"/items/counts/{count.pk}/review/", {"date": "nope"})
    assert b"Enter a date like 2026-01-31." in r.content


def test_cancel_count(std, stocked, admin_user):
    count = start_count(name="Oct", category=None, user=admin_user)
    assert std.get(f"/items/counts/{count.pk}/cancel/").status_code == 405
    r = std.post(f"/items/counts/{count.pk}/cancel/")
    assert r.status_code == 302
    count.refresh_from_db()
    assert count.status == CountStatus.CANCELED


def test_unknown_count_is_404(std, stocked):
    assert std.get("/items/counts/999/").status_code == 404

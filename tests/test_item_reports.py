import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from items.models import CostingMethod, MovementKind
from items.reports import inventory_valuation, negative_stock, stock_movements
from items.stock import (
    StockLine, check_invariants, record_movements, record_value_adjustment, void_movement,
)

D = Decimal
ADJ = MovementKind.ADJUSTMENT


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def mixed(inv_settings, admin_user, make_item):
    a = make_item("A-1")
    b = make_item("B-1", method=CostingMethod.WEIGHTED_AVERAGE)
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user, lines=[
        StockLine(a, 10, unit_cost=D("1.00")), StockLine(b, 5, unit_cost=D("2.00"))])
    record_movements(kind=ADJ, date=d(10, 5), lines=[StockLine(a, -3)], user=admin_user)
    record_value_adjustment(item=b, date=d(10, 6), amount=D("-1.00"), reason="Faded", user=admin_user)
    junk, = record_movements(kind=ADJ, date=d(10, 7), lines=[StockLine(b, 2, unit_cost=D("9.00"))], user=admin_user)
    void_movement(junk, user=admin_user, reason="Typo", date=d(10, 8))
    record_movements(kind=ADJ, date=d(9, 25), lines=[StockLine(a, 10, unit_cost=D("0.80"))], user=admin_user)
    return a, b, junk


@pytest.mark.parametrize("as_of", [d(9, 24), d(9, 25), d(10, 1), d(10, 5), d(10, 7), d(10, 8), d(10, 31)])
def test_valuation_equals_ledger_on_every_date(mixed, as_of):
    v = inventory_valuation(as_of)
    assert v.difference == D("0.00") and v.total == v.ledger_balance


def test_valuation_values_and_quantities(mixed):
    v = inventory_valuation(d(10, 31))
    rows = {r.item.sku: (r.quantity, r.value) for r in v.rows}
    # A-1 FIFO: Sep 25 layer 10 @0.80, Oct 1 layer 10 @1.00, the Oct 5 outflow takes 3 from the 0.80 layer.
    assert rows == {"A-1": (17, D("15.60")), "B-1": (5, D("9.00"))}
    assert v.total == sum((r.value for r in v.rows), D("0.00"))


def test_as_of_before_and_after_a_void(mixed):
    a, b, junk = mixed
    before = {r.item.sku: r.quantity for r in inventory_valuation(d(10, 7)).rows}
    after = {r.item.sku: r.quantity for r in inventory_valuation(d(10, 8)).rows}
    assert before["B-1"] == 7 and after["B-1"] == 5


def test_as_of_before_a_revision_date_excludes_it(inv_settings, admin_user, make_item):
    item = make_item("E-1")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJ, date=d(10, 10), lines=[StockLine(item, -5)], user=admin_user)
    record_movements(kind=ADJ, date=d(9, 25), lines=[StockLine(item, 10, unit_cost=D("0.80"))], user=admin_user)
    assert inventory_valuation(d(10, 9)).total == D("18.00")   # 8.00 + 10.00, revision not yet dated
    assert inventory_valuation(d(10, 10)).total == D("14.00")  # outflow -5.00 plus revision +1.00
    assert inventory_valuation(d(10, 10)).difference == D("0.00")


def test_zero_rows_are_omitted(inv_settings, admin_user, make_item):
    item = make_item("GONE-1")
    record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, 2, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJ, date=d(10, 2), lines=[StockLine(item, -2)], user=admin_user)
    assert inventory_valuation(d(10, 31)).rows == []


def test_stock_movements_running_quantity(mixed):
    a, b, junk = mixed
    rows = stock_movements(d(10, 1), d(10, 31), a)
    assert [(r.movement.kind, r.movement.quantity, r.running_quantity) for r in rows] == [
        (MovementKind.OPENING, 10, 20), (MovementKind.ADJUSTMENT, -3, 17)]  # 10 from the Sep 25 backdated inflow before the range
    assert rows[1].effective_value == D("-2.40")  # booked at 3.00, corrected by +0.60 when the 0.80 layer arrived


def test_stock_movements_all_items_includes_voided_rows(mixed):
    a, b, junk = mixed
    rows = stock_movements(d(10, 1), d(10, 31))
    assert [r.movement.item.sku for r in rows] == ["A-1", "A-1", "B-1", "B-1", "B-1"]
    voided = [r for r in rows if r.movement.voided]
    assert len(voided) == 1 and voided[0].running_quantity == 5


def test_negative_stock_report(inv_settings, admin_user, make_item):
    item = make_item("NEG-1")
    make_item("OK-1")
    record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, -3)], user=admin_user, allow_negative=True)
    (row,) = negative_stock()
    assert (row.item, row.quantity, row.value) == (item, -3, D("-3.00"))


# ---- views ----

@pytest.fixture
def logged_in(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("path", ["/reports/inventory-valuation/", "/reports/stock-movements/", "/reports/negative-stock/"])
def test_item_reports_require_login(client, path):
    assert client.get(path).status_code == 302


def test_standard_user_can_view_item_reports(client, standard_user, mixed):
    client.force_login(standard_user)
    for path in ("/reports/inventory-valuation/?as_of=2026-10-31",
                 "/reports/stock-movements/?start=2026-10-01&end=2026-10-31", "/reports/negative-stock/"):
        assert client.get(path).status_code == 200


def test_valuation_page_shows_cards_and_no_error_banner(logged_in, mixed):
    html = logged_in.get("/reports/inventory-valuation/?as_of=2026-10-31").content.decode()
    assert "A-1" in html and "Stock total" in html and "Inventory accounts" in html and "Difference" in html
    assert "don't match" not in html and "don&#x27;t match" not in html


def test_valuation_page_shows_error_banner_on_mismatch(logged_in, mixed, admin_user, cash, inventory_account):
    from ledger.posting import LineSpec, post_entry

    post_entry(date=d(10, 2), user=admin_user, source_type="items.movement", source_id="999", lines=[
        LineSpec(inventory_account, debit=D("1.00")), LineSpec(cash, credit=D("1.00"))])
    html = logged_in.get("/reports/inventory-valuation/?as_of=2026-10-31").content.decode()
    assert "Stock value and the inventory accounts don" in html and "Difference: -1.00." in html


def test_valuation_csv(logged_in, mixed):
    r = logged_in.get("/reports/inventory-valuation/?as_of=2026-10-31&format=csv")
    assert r["Content-Type"].startswith("text/csv")
    assert 'filename="inventory-valuation-2026-10-31.csv"' in r["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0] == ["SKU", "Name", "Quantity", "Value"] and rows[1][0] == "A-1" and "," not in rows[1][3]
    assert rows[-1][1] == "Total"


def test_movements_csv_and_item_filter(logged_in, mixed):
    r = logged_in.get("/reports/stock-movements/?start=2026-10-01&end=2026-10-31&item=a-1&format=csv")
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert [row[1] for row in rows[1:]] == ["A-1", "A-1"]
    bad = logged_in.get("/reports/stock-movements/?start=2026-10-01&end=2026-10-31&item=NOPE")
    assert b"No item has SKU NOPE." in bad.content


def test_negative_stock_csv(logged_in, inv_settings, admin_user, make_item):
    item = make_item("NEG-2")
    record_movements(kind=ADJ, date=d(10, 1), lines=[StockLine(item, -1)], user=admin_user, allow_negative=True)
    rows = list(csv.reader(io.StringIO(logged_in.get("/reports/negative-stock/?format=csv").content.decode())))
    assert rows[1][:3] == ["NEG-2", "Item NEG-2", "-1"]


def test_bad_date_shows_message(logged_in):
    r = logged_in.get("/reports/inventory-valuation/?as_of=31/10/2026")
    assert b"Enter a date like 2026-01-31." in r.content


def test_seed_demo_builds_consistent_books(db):
    from django.core.management import call_command
    from django.utils import timezone

    call_command("seed_demo", verbosity=0)
    assert check_invariants() == []
    v = inventory_valuation(timezone.localdate())
    assert v.difference == D("0.00") and len(v.rows) >= 30
    assert len(negative_stock()) == 1

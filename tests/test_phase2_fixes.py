"""Fixes from the Phase 2 review: FIFO write-down spread, void warning, grouped import entries, close check."""
from datetime import date
from decimal import Decimal as D

import pytest

from items.costing import Move, replay
from items.errors import NegativeStockWarning
from items.imports import apply_import, parse_items_csv
from items.models import CostingMethod, FifoLayer, MovementKind, StockMovement
from items.reports import inventory_valuation
from items.stock import StockLine, check_invariants, on_hand, record_movements, record_value_adjustment, void_movement
from ledger.errors import PeriodCloseBlockedError
from ledger.models import AccountingPeriod, JournalEntry
from ledger.periods import close_period

FIFO = CostingMethod.FIFO


def _in(kind, d, item, q, user, cost):
    return record_movements(kind=kind, date=d, lines=[StockLine(item, q, unit_cost=cost)], user=user)[0]


# ---- 1. FIFO write-downs never push a batch below zero ----

def test_fifo_writedown_spreads_by_value():
    r = replay(FIFO, [Move(1, date(2026, 10, 1), 1, D("0.10")), Move(2, date(2026, 10, 2), 9, D("10.00")),
                      Move(3, date(2026, 10, 3), 0, value_change=D("-50.00"))], D("0"))
    assert [l.remaining_value for l in r.layers] == [D("0.04"), D("40.06")]
    assert r.value == D("40.10")


def test_fifo_writedown_to_zero_empties_every_batch():
    r = replay(FIFO, [Move(1, date(2026, 10, 1), 1, D("0.10")), Move(2, date(2026, 10, 2), 9, D("10.00")),
                      Move(3, date(2026, 10, 3), 0, value_change=D("-90.10"))], D("0"))
    assert [l.remaining_value for l in r.layers] == [D("0.00"), D("0.00")]


def test_fifo_writeup_on_zero_value_stock_spreads_by_quantity():
    r = replay(FIFO, [Move(1, date(2026, 10, 1), 2, D("0")), Move(2, date(2026, 10, 2), 0, value_change=D("1.00"))], D("0"))
    assert [l.remaining_value for l in r.layers] == [D("1.00")]


def test_selling_after_large_writedown_never_has_negative_cost(inv_settings, admin_user, make_item):
    item = make_item("F1")
    _in(MovementKind.OPENING, date(2026, 10, 1), item, 1, admin_user, D("0.10"))
    _in(MovementKind.OPENING, date(2026, 10, 2), item, 9, admin_user, D("10.00"))
    record_value_adjustment(item=item, date=date(2026, 10, 3), amount=D("-50.00"), reason="Damaged", user=admin_user)
    assert all(l.remaining_value >= 0 for l in FifoLayer.objects.filter(item=item))
    out = record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 4),
                           lines=[StockLine(item, -1)], user=admin_user)[0]
    assert out.total_cost == D("-0.04")
    assert check_invariants() == []


# ---- 2. Voiding a movement warns before taking stock below zero ----

def test_void_that_goes_negative_needs_confirmation(inv_settings, admin_user, make_item):
    item = make_item("V1")
    inflow = _in(MovementKind.OPENING, date(2026, 10, 1), item, 10, admin_user, D("1.00"))
    record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 2), lines=[StockLine(item, -8)], user=admin_user)
    with pytest.raises(NegativeStockWarning) as e:
        void_movement(inflow, user=admin_user, reason="Entered twice")
    assert e.value.messages == ["V1 has 2 on hand. Voiding this movement will take it to -8. Continue?"]
    inflow.refresh_from_db()
    assert not inflow.voided
    void_movement(inflow, user=admin_user, reason="Entered twice", allow_negative=True)
    assert on_hand(item) == -8 and check_invariants() == []


def test_void_of_outflow_never_warns(inv_settings, admin_user, make_item):
    item = make_item("V2")
    _in(MovementKind.OPENING, date(2026, 10, 1), item, 1, admin_user, D("1.00"))
    out = record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 2), lines=[StockLine(item, -3)],
                           user=admin_user, allow_negative=True)[0]
    void_movement(out, user=admin_user, reason="Wrong item")
    assert on_hand(item) == 1


def test_void_view_shows_warning_and_void_anyway(client, inv_settings, admin_user, make_item):
    item = make_item("V3")
    inflow = _in(MovementKind.OPENING, date(2026, 10, 1), item, 5, admin_user, D("1.00"))
    record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 2), lines=[StockLine(item, -5)], user=admin_user)
    client.force_login(admin_user)
    url = f"/items/movements/{inflow.pk}/void/"
    r = client.post(url, {"date": "2026-10-03", "reason": "Entered twice"})
    assert b"Voiding this movement will take it to -5. Continue?" in r.content and b"Void anyway" in r.content
    inflow.refresh_from_db()
    assert not inflow.voided
    r = client.post(url, {"date": "2026-10-03", "reason": "Entered twice", "allow_negative": "1"})
    assert r.status_code == 302
    inflow.refresh_from_db()
    assert inflow.voided


# ---- 3. Imported opening stock posts one entry per date ----

CSV = """sku,name,item_type,category,unit,sale_price,purchase_cost,vendor_part_number,costing_method,opening_quantity,opening_unit_cost,opening_date
A-1,Alpha,stock,Bolts,each,1,0.50,,fifo,10,0.50,2026-10-01
B-1,Beta,stock,Bolts,each,1,0.75,,weighted average,4,0.75,2026-10-01
C-1,Gamma,stock,Paint,each,9,3.00,,,2,3.00,2026-10-02
"""


def test_import_posts_one_entry_per_date(inv_settings, admin_user):
    apply_import(parse_items_csv(CSV), user=admin_user)
    entries = JournalEntry.objects.filter(source_type="items.import").order_by("date")
    assert [(e.date, e.lines.count()) for e in entries] == [(date(2026, 10, 1), 2), (date(2026, 10, 2), 2)]
    first = entries[0]
    assert sorted(l.debit for l in first.lines.all()) == [D("0.00"), D("8.00")]
    openings = StockMovement.objects.filter(kind=MovementKind.OPENING, date=date(2026, 10, 1))
    assert {m.journal_entry_id for m in openings} == {first.pk}
    assert check_invariants() == [] and inventory_valuation(date(2026, 10, 31)).difference == 0


def test_voiding_one_imported_item_leaves_the_others(inv_settings, admin_user):
    apply_import(parse_items_csv(CSV), user=admin_user)
    alpha = StockMovement.objects.get(item__sku="A-1")
    void_movement(alpha, user=admin_user, reason="Wrong count")
    assert on_hand(StockMovement.objects.get(item__sku="B-1").item) == 4
    assert check_invariants() == [] and inventory_valuation(date(2026, 10, 31)).difference == 0


# ---- 4. A month can't close while inventory and the ledger disagree ----

def test_close_blocked_when_inventory_does_not_match(monkeypatch, inv_settings, admin_user, make_item):
    item = make_item("K1")
    _in(MovementKind.OPENING, date(2026, 10, 1), item, 2, admin_user, D("1.00"))
    from items import reports
    real = reports.inventory_valuation

    def off_by_a_cent(as_of):
        v = real(as_of)
        return v.__class__(v.as_of, v.rows, v.total + D("0.01"), v.ledger_balance, D("0.01"))

    monkeypatch.setattr(reports, "inventory_valuation", off_by_a_cent)
    with pytest.raises(PeriodCloseBlockedError, match=r"October 2026 can't be closed: stock value and the inventory accounts differ by 0.01 at month end."):
        close_period(2026, 10, admin_user)
    assert not AccountingPeriod.objects.filter(year=2026, month=10, is_closed=True).exists()


def test_close_allowed_when_inventory_matches(inv_settings, admin_user, make_item):
    item = make_item("K2")
    _in(MovementKind.OPENING, date(2026, 10, 1), item, 2, admin_user, D("1.00"))
    assert close_period(2026, 10, admin_user).is_closed


def test_admin_close_action_shows_blocking_message(client, monkeypatch, inv_settings, admin_user, make_item):
    from django.contrib.auth import get_user_model

    item = make_item("K3")
    _in(MovementKind.OPENING, date(2026, 10, 1), item, 2, admin_user, D("1.00"))
    from items import reports
    monkeypatch.setattr(reports, "inventory_valuation", lambda as_of: type("V", (), {"difference": D("2.50")})())
    period = AccountingPeriod.objects.get_or_create(year=2026, month=10)[0]
    admin_user.is_superuser = True
    admin_user.save()
    client.force_login(admin_user)
    r = client.post("/admin/ledger/accountingperiod/", {"action": "close_periods", "_selected_action": [period.pk]}, follow=True)
    assert b"differ by 2.50 at month end" in r.content
    period.refresh_from_db()
    assert not period.is_closed

import uuid
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from items.models import MovementKind, StockMovement, SubmittedToken
from items.stock import StockLine, check_invariants, on_hand, record_movements, stock_value
from ledger.periods import close_period

D = Decimal
ADJ = MovementKind.ADJUSTMENT


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def stocked(inv_settings, admin_user, make_item):
    item = make_item("BOLT-1", name="Hex bolt")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    return item


def logged(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def std(standard_user):
    return logged(standard_user)


@pytest.fixture
def adm(admin_user):
    return logged(admin_user)


def post_data(token=None, **over):
    data = {"date": "2026-10-05", "token": token or str(uuid.uuid4()),
            "lines-0-sku": "BOLT-1", "lines-0-quantity": "-3", "lines-0-reason": "Damaged", "lines-0-note": "", "lines-0-account": ""}
    data.update(over)
    return data


def test_adjust_form_prefills_item_and_has_token(std, stocked):
    html = std.get("/items/adjust/?item=bolt-1").content.decode()
    assert 'value="BOLT-1"' in html and 'name="token"' in html and "Damaged" in html


def test_adjust_posts_once_and_redirects_to_item(std, stocked):
    r = std.post("/items/adjust/", post_data(**{"lines-0-note": "dented"}), follow=True)
    assert r.redirect_chain[-1][0] == "/items/BOLT-1/"
    assert on_hand(stocked) == 7
    m = StockMovement.objects.get(kind=ADJ)
    assert m.reason == "Damaged: dented" and m.offset_account.number == "6500" and m.created_by.username == "standard"
    assert b"Saved 1 adjustment." in r.content
    assert check_invariants() == []


def test_multiple_lines_redirect_to_adjustments_list(std, stocked, make_item):
    make_item("NUT-1")
    data = post_data(**{"lines-1-sku": "nut-1", "lines-1-quantity": "5", "lines-1-reason": "Correction", "lines-1-note": "", "lines-1-account": ""})
    r = std.post("/items/adjust/", data)
    assert r.status_code == 302 and r["Location"] == "/items/adjustments/"
    assert StockMovement.objects.filter(kind=ADJ).count() == 2


def test_negative_shows_confirmation_and_saves_nothing_until_confirmed(std, stocked):
    data = post_data(**{"lines-0-quantity": "-12"})
    r = std.post("/items/adjust/", data)
    html = r.content.decode()
    assert r.status_code == 200 and "Line 1: BOLT-1 has 10 on hand. Saving will take it to -2. Continue?" in html
    assert "Save anyway" in html and "Go back" in html
    assert StockMovement.objects.filter(kind=ADJ).count() == 0
    r = std.post("/items/adjust/", {**data, "allow_negative": "1"})
    assert r.status_code == 302 and on_hand(stocked) == -2


def test_go_back_returns_the_form_with_values(std, stocked):
    r = std.post("/items/adjust/", post_data(**{"lines-0-quantity": "-12", "back": "1"}))
    html = r.content.decode()
    assert 'value="-12"' in html and "Save anyway" not in html


def test_same_token_twice_posts_once(std, stocked):  # Review Focus 2
    data = post_data()
    std.post("/items/adjust/", data)
    r = std.post("/items/adjust/", data, follow=True)
    assert on_hand(stocked) == 7 and StockMovement.objects.filter(kind=ADJ).count() == 1
    assert b"This adjustment was already saved." in r.content
    assert SubmittedToken.objects.count() == 1


def test_failed_post_does_not_burn_the_token(std, stocked):
    data = post_data(**{"lines-0-quantity": "-12"})
    std.post("/items/adjust/", data)
    assert SubmittedToken.objects.count() == 0


def test_unknown_sku_and_bad_quantity_messages(std, stocked):
    r = std.post("/items/adjust/", post_data(**{"lines-0-sku": "NOPE"}))
    assert b"Line 1: no item has SKU NOPE." in r.content
    r = std.post("/items/adjust/", post_data(**{"lines-0-quantity": "2.5"}))
    assert b"Line 1: quantity must be a whole number." in r.content
    r = std.post("/items/adjust/", post_data(**{"lines-0-quantity": "abc"}))
    assert b"Line 1: quantity must be a whole number." in r.content
    r = std.post("/items/adjust/", post_data(**{"lines-0-quantity": "0"}))
    assert b"Line 1: enter a quantity other than zero." in r.content
    assert StockMovement.objects.filter(kind=ADJ).count() == 0


def test_settings_missing_banner_links_to_settings(std, make_item):  # Review Focus 1
    make_item("BOLT-1")
    r = std.post("/items/adjust/", post_data())
    html = r.content.decode()
    assert r.status_code == 200 and "Set up inventory accounts first." in html and 'href="/items/settings/"' in html
    assert StockMovement.objects.count() == 0


def test_closed_month_shows_period_message(std, stocked, admin_user):
    close_period(2026, 10, admin_user)
    r = std.post("/items/adjust/", post_data())
    assert b"October 2026 is closed." in r.content and StockMovement.objects.filter(kind=ADJ).count() == 0


def test_bad_date_message(std, stocked):
    r = std.post("/items/adjust/", post_data(date="10/05/2026"))
    assert b"Enter a date like 2026-01-31." in r.content


def test_adjust_requires_login(client):
    assert client.get("/items/adjust/").status_code == 302


# ---- adjustments list ----

def test_adjustments_list_shows_adjustments_and_value_adjustments(std, adm, stocked, admin_user):
    from items.stock import record_value_adjustment

    record_movements(kind=ADJ, date=d(10, 5), lines=[StockLine(stocked, -2, reason="Lost")], user=admin_user)
    record_value_adjustment(item=stocked, date=d(10, 6), amount=D("-1.00"), reason="Faded", user=admin_user)
    html = std.get("/items/adjustments/").content.decode()
    assert "BOLT-1" in html and "Lost" in html and "Faded" in html and "Opening stock" not in html
    assert "/void/" not in html                      # void links are admin only
    assert "/void/" in adm.get("/items/adjustments/").content.decode()


# ---- value adjustment ----

def test_value_adjust_admin_only(std, stocked):
    assert std.get("/items/BOLT-1/value-adjust/").status_code == 403
    assert std.post("/items/BOLT-1/value-adjust/", {"date": "2026-10-05", "mode": "change", "amount": "-1.00", "reason": "x"}).status_code == 403


def test_value_adjust_change_by(adm, stocked):
    data = {"date": "2026-10-05", "mode": "change", "amount": "-2.50", "reason": "Faded", "token": str(uuid.uuid4())}
    r = adm.post("/items/BOLT-1/value-adjust/", data)
    assert r.status_code == 302 and r["Location"] == "/items/BOLT-1/"
    assert stock_value(stocked) == D("7.50") and check_invariants() == []


def test_value_adjust_new_total(adm, stocked):
    data = {"date": "2026-10-05", "mode": "total", "amount": "6.00", "reason": "Repriced", "token": str(uuid.uuid4())}
    adm.post("/items/BOLT-1/value-adjust/", data)
    assert stock_value(stocked) == D("6.00")


def test_value_adjust_same_token_once(adm, stocked):
    data = {"date": "2026-10-05", "mode": "change", "amount": "-1.00", "reason": "Faded", "token": str(uuid.uuid4())}
    adm.post("/items/BOLT-1/value-adjust/", data)
    r = adm.post("/items/BOLT-1/value-adjust/", data, follow=True)
    assert stock_value(stocked) == D("9.00") and b"This adjustment was already saved." in r.content


def test_value_adjust_blocked_without_stock(adm, inv_settings, make_item):
    make_item("EMPTY-1")
    r = adm.post("/items/EMPTY-1/value-adjust/", {"date": "2026-10-05", "mode": "change", "amount": "1.00", "reason": "x"})
    assert r.status_code == 200 and b"EMPTY-1 has no stock to revalue." in r.content


def test_value_adjust_requires_reason_and_valid_amount(adm, stocked):
    r = adm.post("/items/BOLT-1/value-adjust/", {"date": "2026-10-05", "mode": "change", "amount": "1.00", "reason": ""})
    assert b"Enter a reason." in r.content
    r = adm.post("/items/BOLT-1/value-adjust/", {"date": "2026-10-05", "mode": "change", "amount": "1.005", "reason": "x"})
    assert b"Enter an amount with at most 2 decimal places." in r.content
    r = adm.post("/items/BOLT-1/value-adjust/", {"date": "2026-10-05", "mode": "total", "amount": "10.00", "reason": "x"})
    assert b"Enter an amount other than zero." in r.content or b"already" in r.content
    assert stock_value(stocked) == D("10.00")


# ---- void ----

def test_void_admin_only(std, stocked):
    m = StockMovement.objects.get()
    assert std.get(f"/items/movements/{m.pk}/void/").status_code == 403


def test_void_movement_screen(adm, stocked):
    m = StockMovement.objects.get()
    assert b"Reason" in adm.get(f"/items/movements/{m.pk}/void/").content
    r = adm.post(f"/items/movements/{m.pk}/void/", {"date": "2026-10-06", "reason": "Entered twice"})
    assert r.status_code == 302 and r["Location"] == "/items/BOLT-1/"
    m.refresh_from_db()
    assert m.voided and on_hand(stocked) == 0 and check_invariants() == []


def test_void_errors_show_inline(adm, stocked, admin_user):
    m = StockMovement.objects.get()
    r = adm.post(f"/items/movements/{m.pk}/void/", {"date": "2026-10-06", "reason": ""})
    assert b"Enter a reason." in r.content
    close_period(2026, 10, admin_user)
    r = adm.post(f"/items/movements/{m.pk}/void/", {"date": "2026-10-06", "reason": "x"})
    assert r.status_code == 200 and b"October 2026 is closed." in r.content
    assert not StockMovement.objects.get(pk=m.pk).voided


def test_void_unknown_movement_is_404(adm, stocked):
    assert adm.get("/items/movements/9999/void/").status_code == 404

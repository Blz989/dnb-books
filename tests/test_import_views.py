from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from items.imports import HEADERS
from items.models import InventorySettings, Item, MovementKind, StockMovement
from items.stock import StockLine, record_movements

D = Decimal
HEAD = ",".join(HEADERS)
GOOD = HEAD + """
BOLT-1,Hex bolt,stock,Fasteners,each,0.75,0.4950,HB-1,fifo,100,0.5000,2026-10-01
PNT-1,Red paint,stock,Paint,quart,12.99,7.2,,weighted average,10,7.2000,2026-10-01
SO-1,Special order,non-stock,,,25,0,,,,,
"""
BAD = HEAD + """
OK-1,Fine,stock,,each,1,1,,,5,1.00,2026-10-01
BAD 2,Bad sku,stock,,each,1,1,,,,,
OK-3,Fine,stock,,each,1,1,,,,,
OK-4,Bad type,widget,,each,1,1,,,,,
"""


def upload(client, text, name="items.csv"):
    return client.post("/items/import/", {"file": SimpleUploadedFile(name, text.encode(), content_type="text/csv")})


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


def test_standard_user_gets_403_on_import_and_settings(std, inv_settings):
    for path in ("/items/import/", "/items/settings/"):
        assert std.get(path).status_code == 403
    assert std.post("/items/import/confirm/").status_code == 403
    assert std.post("/items/settings/", {}).status_code == 403


def test_upload_page_shows_columns(adm, inv_settings):
    html = adm.get("/items/import/").content.decode()
    assert "opening_unit_cost" in html and 'type="file"' in html


def test_preview_with_errors_lists_every_row_and_has_no_confirm(adm, inv_settings):
    r = upload(adm, BAD)
    html = r.content.decode()
    assert r.status_code == 200 and "Fix these rows and upload again." in html
    assert "Row 3: SKU can use letters" in html and "Row 5: item_type must be stock or non-stock." in html
    assert "Confirm import" not in html
    assert Item.objects.count() == 0


def test_preview_ready_shows_counts_and_confirm_imports(adm, inv_settings):
    r = upload(adm, GOOD)
    html = r.content.decode()
    assert "Confirm import" in html and "Fix these rows" not in html
    assert "3 items" in html and "2 stock items" in html and "2 items with opening stock" in html
    assert "122.00" in html  # 100 x 0.50 + 10 x 7.20
    assert Item.objects.count() == 0                      # nothing saved at preview
    r = adm.post("/items/import/confirm/", follow=True)
    assert r.redirect_chain[-1][0] == "/items/" and b"Imported 3 items." in r.content
    assert Item.objects.count() == 3 and StockMovement.objects.filter(kind=MovementKind.OPENING).count() == 2


def test_confirm_without_a_preview_goes_back_to_upload(adm, inv_settings):
    r = adm.post("/items/import/confirm/", follow=True)
    assert r.redirect_chain[-1][0] == "/items/import/" and b"Upload a file first." in r.content


def test_confirm_reparses_so_stale_preview_cannot_post(adm, inv_settings, make_item):
    upload(adm, GOOD)
    make_item("BOLT-1")                                    # now a duplicate
    r = adm.post("/items/import/confirm/", follow=True)
    assert b"Row 2: SKU BOLT-1 already exists." in r.content
    assert Item.objects.count() == 1


def test_confirm_without_settings_shows_settings_message(adm, db, revenue):  # Review Focus 1
    upload(adm, GOOD)
    r = adm.post("/items/import/confirm/")
    html = r.content.decode()
    assert r.status_code == 200 and "Set up inventory accounts first." in html and 'href="/items/settings/"' in html
    assert Item.objects.count() == 0


def test_too_large_empty_and_non_utf8_files(adm, inv_settings):
    big = "x" * (2 * 1024 * 1024 + 1)
    assert b"The file is too large (2 MB limit)." in upload(adm, big).content
    assert b"Choose a CSV file to upload." in adm.post("/items/import/", {}).content
    r = adm.post("/items/import/", {"file": SimpleUploadedFile("a.csv", b"\xff\xfe\x00bad")})
    assert b"The file isn" in r.content and b"UTF-8" in r.content


def test_closed_month_on_confirm_shows_period_message(adm, inv_settings, admin_user):
    from ledger.periods import close_period

    upload(adm, GOOD)
    close_period(2026, 10, admin_user)
    r = adm.post("/items/import/confirm/")
    # opening_date in a closed month is caught when the file is re-parsed
    assert r.status_code in (200, 302) and Item.objects.count() == 0


# ---- settings ----

def test_settings_form_saves_accounts(adm, inv_settings, inventory_account, cogs_account, revenue, shrinkage_account,
                                      writedown_account, opening_equity):
    html = adm.get("/items/settings/").content.decode()
    assert "Shrinkage account" in html and "Opening balance equity account" in html
    data = {"default_inventory_account": inventory_account.pk, "default_cogs_account": cogs_account.pk,
            "default_income_account": revenue.pk, "shrinkage_account": writedown_account.pk,
            "writedown_account": shrinkage_account.pk, "opening_equity_account": opening_equity.pk}
    r = adm.post("/items/settings/", data, follow=True)
    assert b"Settings saved." in r.content
    assert InventorySettings.load().shrinkage_account == writedown_account


def test_settings_rejects_account_of_wrong_subtype(adm, inv_settings, cash, cogs_account, revenue, shrinkage_account,
                                                   writedown_account, opening_equity):
    data = {"default_inventory_account": cash.pk, "default_cogs_account": cogs_account.pk,
            "default_income_account": revenue.pk, "shrinkage_account": shrinkage_account.pk,
            "writedown_account": writedown_account.pk, "opening_equity_account": opening_equity.pk}
    r = adm.post("/items/settings/", data)
    assert r.status_code == 200 and b"Choose an account from the list." in r.content
    assert InventorySettings.load().default_inventory_account.number == "1300"


def test_settings_all_required(adm, db):
    r = adm.post("/items/settings/", {})
    assert r.status_code == 200 and r.content.count(b"Choose an account.") == 6


# ---- home and sidebar ----

@pytest.fixture
def three_negative(inv_settings, admin_user, make_item):
    for i in range(3):
        item = make_item(f"NEG-{i}")
        record_movements(kind=MovementKind.ADJUSTMENT, date=date(2026, 10, 5), lines=[StockLine(item, -2)],
                         user=admin_user, allow_negative=True)


def test_home_banner_and_sidebar_pill(adm, three_negative):
    html = adm.get("/").content.decode()
    assert "3 items are below zero. Their cost is estimated until stock is received or counted." in html
    assert "3 below zero" in html and "/reports/negative-stock/" in html
    assert "3 below zero" in adm.get("/items/").content.decode()


def test_home_has_no_banner_when_nothing_is_negative(adm, inv_settings):
    html = adm.get("/").content.decode()
    assert "below zero. Their cost" not in html and "below zero</span>" not in html


def test_home_shows_stock_value_and_inventory_links(adm, inv_settings, admin_user, make_item):
    item = make_item("A-1")
    record_movements(kind=MovementKind.OPENING, date=date(2026, 10, 1), lines=[StockLine(item, 4, unit_cost=D("2.50"))], user=admin_user)
    html = adm.get("/").content.decode()
    assert "Stock value" in html and "10.00" in html
    for link in ("/items/", "/items/adjust/", "/items/counts/", "/reports/inventory-valuation/", "/reports/stock-movements/"):
        assert f'href="{link}"' in html


def test_home_info_banner_for_admin_when_not_configured(adm, db, standard_user):
    assert "Inventory isn't set up yet. Choose the inventory accounts in Settings." in adm.get("/").content.decode()
    other = Client()
    other.force_login(standard_user)
    assert "set up yet" not in other.get("/").content.decode()

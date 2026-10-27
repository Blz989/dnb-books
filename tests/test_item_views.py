from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from items.models import CostingMethod, CostRevision, Item, ItemType, MovementKind
from items.stock import StockLine, record_movements, record_value_adjustment

D = Decimal
ADJ = MovementKind.ADJUSTMENT


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def stocked(inv_settings, admin_user, make_item):
    a = make_item("BOLT-14", vendor_part_number="HB-014", name='Hex bolt 1/4"')
    b = make_item("PNT-1", method=CostingMethod.WEIGHTED_AVERAGE, name="Red paint")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user, lines=[
        StockLine(a, 10, unit_cost=D("1.00")), StockLine(b, 4, unit_cost=D("7.25"))])
    return a, b


@pytest.fixture
def std(client, standard_user):
    client.force_login(standard_user)
    return client


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


def form_data(**over):
    data = {"sku": "NEW-1", "name": "New item", "description": "", "item_type": "STOCK", "category": "",
            "unit": "each", "sale_price": "2.50", "purchase_cost": "1.25", "vendor_part_number": "",
            "costing_method": "FIFO"}
    data.update(over)
    return data


def account_fields(inv_settings):
    return {"income_account": inv_settings.default_income_account_id, "cogs_account": inv_settings.default_cogs_account_id,
            "inventory_account": inv_settings.default_inventory_account_id}


# ---- list ----

def test_items_require_login(client):
    assert client.get("/items/").status_code == 302


def test_list_shows_columns_and_kpis(std, stocked):
    html = std.get("/items/").content.decode()
    assert "BOLT-14" in html and "PNT-1" in html
    assert "Stock value" in html and "39.00" in html  # 10 x 1.00 + 4 x 7.25
    assert "FIFO" in html and "Avg" in html and "7.2500" in html


def test_search_matches_vendor_part_number(std, stocked):
    html = std.get("/items/?q=hb-014").content.decode()
    assert "BOLT-14" in html and "PNT-1" not in html


def test_search_matches_name_and_sku_case_insensitively(std, stocked):
    assert "PNT-1" in std.get("/items/?q=RED PAINT").content.decode()
    assert "BOLT-14" in std.get("/items/?q=bolt").content.decode()


def test_filter_by_category_and_type(std, stocked, inv_settings, revenue):
    from items.models import Category

    cat = Category.objects.create(name="Paint")
    Item.objects.filter(sku="PNT-1").update(category=cat)
    Item.objects.create(sku="SO-1", name="Special order", item_type=ItemType.NON_STOCK, income_account=revenue)
    assert "PNT-1" in std.get(f"/items/?category={cat.pk}").content.decode()
    assert "BOLT-14" not in std.get(f"/items/?category={cat.pk}").content.decode()
    ns = std.get("/items/?type=non-stock").content.decode()
    assert "SO-1" in ns and "BOLT-14" not in ns
    st = std.get("/items/?type=stock").content.decode()
    assert "SO-1" not in st and "BOLT-14" in st


def test_inactive_items_hidden_until_requested(std, stocked, make_item):
    make_item("OLD-1", is_active=False)
    assert "OLD-1" not in std.get("/items/").content.decode()
    assert "OLD-1" in std.get("/items/?inactive=1").content.decode()


def test_pagination_50_per_page(std, inv_settings, make_item):
    for i in range(55):
        make_item(f"P-{i:03d}")
    page1 = std.get("/items/").content.decode()
    assert "P-049" in page1 and "P-050" not in page1 and "Showing 1" in page1
    page2 = std.get("/items/?page=2").content.decode()
    assert "P-050" in page2 and "P-049" not in page2
    assert std.get("/items/?page=999").status_code == 200


def test_non_stock_item_shows_dashes(std, revenue):
    Item.objects.create(sku="SO-1", name="Special order", item_type=ItemType.NON_STOCK, income_account=revenue)
    html = std.get("/items/").content.decode()
    assert "Non-stock" in html and html.count("—") >= 3


def test_negative_stock_banner_and_neg_class(std, stocked, admin_user):
    a, b = stocked
    record_movements(kind=ADJ, date=d(10, 5), lines=[StockLine(b, -6)], user=admin_user, allow_negative=True)
    html = std.get("/items/").content.decode()
    assert "1 item is below zero" in html and "/reports/negative-stock/" in html and 'class="num neg"' in html


# ---- detail ----

def test_detail_case_insensitive_sku_and_history(std, stocked, admin_user):
    a, b = stocked
    record_movements(kind=ADJ, date=d(10, 5), lines=[StockLine(a, -3, reason="Damaged")], user=admin_user)
    record_movements(kind=ADJ, date=d(9, 25), lines=[StockLine(a, 10, unit_cost=D("0.80"))], user=admin_user)
    r = std.get("/items/bolt-14/")
    html = r.content.decode()
    assert r.status_code == 200 and "BOLT-14" in html and "Damaged" in html and "HB-014" in html
    assert "Opening stock" in html and "Quantity adjustment" in html
    assert "Cost correction" in html and "0.60" in html  # revision row under the Oct 5 outflow
    assert "JE-0000" in html and "/admin/ledger/journalentry/" in html
    assert "on hand" in html.lower()


def test_detail_unknown_sku_is_404_in_app_style(std, stocked):
    r = std.get("/items/nope/")
    assert r.status_code == 404 and b"DNB Books" in r.content and b"Not found" in r.content


def test_detail_shows_voided_movements_struck_through(std, stocked, admin_user):
    from items.stock import void_movement

    a, b = stocked
    m, = record_movements(kind=ADJ, date=d(10, 5), lines=[StockLine(a, 2, unit_cost=D("1.00"))], user=admin_user)
    void_movement(m, user=admin_user, reason="Typo")
    html = std.get("/items/BOLT-14/").content.decode()
    assert "Voided" in html and "line-through" in html


def test_detail_value_adjust_button_admin_only(stocked, standard_user, admin_user):
    from django.test import Client

    standard, admin = Client(), Client()
    standard.force_login(standard_user)
    admin.force_login(admin_user)
    assert "value-adjust" not in standard.get("/items/BOLT-14/").content.decode()
    assert "value-adjust" in admin.get("/items/BOLT-14/").content.decode()


# ---- create and edit ----

def test_new_item_form_defaults_from_settings(std, inv_settings):
    html = std.get("/items/new/").content.decode()
    assert f'value="{inv_settings.default_inventory_account_id}" selected' in html
    assert f'value="{inv_settings.default_cogs_account_id}" selected' in html
    assert f'value="{inv_settings.default_income_account_id}" selected' in html
    assert 'value="FIFO" selected' in html


def test_standard_user_can_create_and_edit(std, inv_settings):
    r = std.post("/items/new/", form_data(**account_fields(inv_settings)))
    assert r.status_code == 302 and r["Location"] == "/items/NEW-1/"
    item = Item.objects.get(sku="NEW-1")
    assert item.purchase_cost == D("1.2500") and item.costing_method == "FIFO"
    r = std.post("/items/NEW-1/edit/", form_data(name="Renamed", **account_fields(inv_settings), is_active="on"))
    assert r.status_code == 302 and Item.objects.get(sku="NEW-1").name == "Renamed"


def test_non_stock_item_created_without_inventory_fields(std, inv_settings):
    data = form_data(sku="SVC-1", item_type="NON_STOCK", **account_fields(inv_settings))
    assert std.post("/items/new/", data).status_code == 302
    item = Item.objects.get(sku="SVC-1")
    assert item.inventory_account is None and item.cogs_account is None and item.costing_method == ""


@pytest.mark.parametrize("sku", ["BOLT 1", "BOLT/1", "-BOLT"])
def test_bad_sku_shows_message(std, inv_settings, sku):  # Review Focus 4
    r = std.post("/items/new/", form_data(sku=sku, **account_fields(inv_settings)))
    assert r.status_code == 200 and b"SKU can use letters, numbers, dots, dashes, and underscores (up to 50)." in r.content
    assert Item.objects.count() == 0


def test_duplicate_sku_any_case_shows_message(std, stocked, inv_settings):
    r = std.post("/items/new/", form_data(sku="bolt-14", **account_fields(inv_settings)))
    assert r.status_code == 200 and b"SKU bolt-14 already exists." in r.content


def test_reserved_sku_rejected(std, inv_settings):
    r = std.post("/items/new/", form_data(sku="adjust", **account_fields(inv_settings)))
    assert r.status_code == 200 and b"That SKU is reserved." in r.content


def test_stock_item_without_accounts_is_a_form_error(std, inv_settings):
    r = std.post("/items/new/", form_data())
    assert r.status_code == 200 and Item.objects.count() == 0


def test_method_is_read_only_after_first_movement(std, stocked, inv_settings):
    html = std.get("/items/BOLT-14/edit/").content.decode()
    assert 'name="costing_method"' in html and "disabled" in html
    r = std.post("/items/BOLT-14/edit/", form_data(sku="BOLT-14", costing_method="WEIGHTED_AVERAGE",
                                                   **account_fields(inv_settings), is_active="on"))
    assert r.status_code == 200 and b"The costing method can&#x27;t change after an item has stock history." in r.content
    assert Item.objects.get(sku="BOLT-14").costing_method == "FIFO"


def test_method_can_change_before_any_movement(std, make_item, inv_settings):
    make_item("FRESH-1")
    r = std.post("/items/FRESH-1/edit/", form_data(sku="FRESH-1", costing_method="WEIGHTED_AVERAGE",
                                                   **account_fields(inv_settings), is_active="on"))
    assert r.status_code == 302 and Item.objects.get(sku="FRESH-1").costing_method == "WEIGHTED_AVERAGE"


def test_item_type_cannot_change_after_movement(std, stocked, inv_settings):
    r = std.post("/items/BOLT-14/edit/", form_data(sku="BOLT-14", item_type="NON_STOCK",
                                                   **account_fields(inv_settings), is_active="on"))
    assert r.status_code == 200 and b"The item type can" in r.content
    assert Item.objects.get(sku="BOLT-14").item_type == ItemType.STOCK


# ---- deactivate ----

def test_deactivate_with_stock_shows_message(std, stocked):
    r = std.post("/items/BOLT-14/deactivate/", follow=True)
    assert b"BOLT-14 still has 10 on hand. Adjust it to zero before deactivating." in r.content
    assert Item.objects.get(sku="BOLT-14").is_active


def test_deactivate_without_stock(std, make_item):
    make_item("EMPTY-1")
    r = std.post("/items/EMPTY-1/deactivate/")
    assert r.status_code == 302 and not Item.objects.get(sku="EMPTY-1").is_active


def test_deactivate_requires_post(std, make_item):
    make_item("EMPTY-2")
    assert std.get("/items/EMPTY-2/deactivate/").status_code == 405

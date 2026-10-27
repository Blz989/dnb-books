from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError

from items.errors import ItemStateError
from items.models import (
    CostRevision, Category, InventorySettings, Item, ItemType, MovementKind, StockMovement,
)

D = Decimal


@pytest.fixture
def posted_movement(make_item, admin_user, inventory_account, opening_equity):
    item = make_item("BOLT-1")
    return StockMovement.objects.create(
        item=item, date=date(2026, 10, 1), kind=MovementKind.OPENING, quantity=10,
        unit_cost=D("1.0000"), total_cost=D("10.00"), offset_account=opening_equity, created_by=admin_user)


def test_sku_case_insensitive_unique(make_item):
    make_item("BOLT-1")
    with pytest.raises((IntegrityError, ValidationError)):
        make_item("bolt-1")


@pytest.mark.parametrize("sku", ["BOLT 1", "BOLT/1", "-BOLT", ""])
def test_sku_pattern_rejected(sku, make_item):
    with pytest.raises(ValidationError, match="SKU can use letters"):
        make_item(sku)


def test_category_name_case_insensitive_unique(db):
    Category.objects.create(name="Paint")
    with pytest.raises((IntegrityError, ValidationError)):
        Category.objects.create(name="paint")


def test_non_stock_item_has_no_inventory_fields(db, revenue, inventory_account, cogs_account):
    Item.objects.create(sku="SO-1", name="Special", item_type=ItemType.NON_STOCK, income_account=revenue)
    with pytest.raises(ValidationError):
        Item.objects.create(sku="SO-2", name="Special", item_type=ItemType.NON_STOCK, income_account=revenue,
                            inventory_account=inventory_account)
    with pytest.raises(ValidationError):
        Item.objects.create(sku="SO-3", name="Special", item_type=ItemType.NON_STOCK, income_account=revenue,
                            costing_method="FIFO")


def test_stock_item_requires_inventory_and_cogs_accounts(db, revenue, inventory_account, cogs_account):
    with pytest.raises(ValidationError):
        Item.objects.create(sku="S-1", name="S", item_type=ItemType.STOCK, income_account=revenue,
                            costing_method="FIFO", cogs_account=cogs_account)
    with pytest.raises(ValidationError):
        Item.objects.create(sku="S-2", name="S", item_type=ItemType.STOCK, income_account=revenue,
                            costing_method="FIFO", inventory_account=inventory_account)
    with pytest.raises(ValidationError):  # wrong subtype for inventory
        Item.objects.create(sku="S-3", name="S", item_type=ItemType.STOCK, income_account=revenue,
                            costing_method="FIFO", inventory_account=revenue, cogs_account=cogs_account)


def test_item_state_properties(make_item, posted_movement):
    assert posted_movement.item.is_stock and posted_movement.item.has_movements
    assert not make_item("OTHER").has_movements


def test_movement_cannot_be_edited_by_sql(posted_movement):
    with pytest.raises(DatabaseError):
        StockMovement.objects.filter(pk=posted_movement.pk).update(quantity=5)


def test_movement_cannot_be_deleted_by_sql_or_model(posted_movement):
    with pytest.raises(DatabaseError):
        StockMovement.objects.filter(pk=posted_movement.pk).delete()
    with pytest.raises(ItemStateError):
        posted_movement.delete()


def test_movement_model_guard_blocks_edit(posted_movement):
    posted_movement.quantity = 99
    with pytest.raises(ItemStateError, match="Stock movements can't be changed. Void the movement instead."):
        posted_movement.save()


def test_movement_void_fields_can_be_set_once(posted_movement):
    posted_movement.voided = True
    posted_movement.voided_on = date(2026, 10, 2)
    posted_movement.void_amount = D("-10.00")
    posted_movement.save()
    fresh = StockMovement.objects.get(pk=posted_movement.pk)
    assert fresh.voided and fresh.void_amount == D("-10.00")
    fresh.void_amount = D("-5.00")
    with pytest.raises(ItemStateError):
        fresh.save()
    with pytest.raises(DatabaseError):
        StockMovement.objects.filter(pk=fresh.pk).update(void_amount=D("-1.00"))


def test_void_update_by_sql_cannot_change_other_columns(posted_movement):
    with pytest.raises(DatabaseError):
        StockMovement.objects.filter(pk=posted_movement.pk).update(voided=True, quantity=1)


def test_cost_revision_is_immutable(posted_movement):
    r = CostRevision.objects.create(movement=posted_movement, date=date(2026, 10, 1), delta=D("0.50"))
    r.delta = D("1.00")
    with pytest.raises(ItemStateError):
        r.save()
    with pytest.raises(ItemStateError):
        r.delete()
    with pytest.raises(DatabaseError):
        CostRevision.objects.filter(pk=r.pk).update(delta=D("2.00"))
    with pytest.raises(DatabaseError):
        CostRevision.objects.filter(pk=r.pk).delete()


def test_settings_load_is_singleton(db):
    assert InventorySettings.load().pk == InventorySettings.load().pk
    assert InventorySettings.objects.count() == 1
    assert not InventorySettings.load().is_configured


def test_settings_configured_when_all_six_set(inv_settings):
    assert InventorySettings.load().is_configured


def test_roles_standard_edits_items_but_not_settings(standard_user, admin_user):
    assert standard_user.has_perm("items.add_item") and standard_user.has_perm("items.add_stockcount")
    assert standard_user.has_perm("items.view_stockmovement")
    assert not standard_user.has_perm("items.change_inventorysettings")
    assert not standard_user.has_perm("items.add_stockmovement")
    assert admin_user.has_perm("items.change_inventorysettings")

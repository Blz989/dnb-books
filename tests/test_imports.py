from datetime import date
from decimal import Decimal

import pytest

from items.errors import SettingsMissingError
from items.imports import HEADERS, apply_import, parse_items_csv
from items.models import Category, CostingMethod, Item, ItemType, MovementKind, StockMovement
from items.stock import check_invariants, on_hand, stock_value
from ledger.periods import close_period

D = Decimal
HEAD = ",".join(HEADERS)

GOOD = HEAD + """
BOLT-1,Hex bolt,stock,Fasteners,each,0.75,0.4950,HB-1,fifo,100,0.5000,2026-10-01
PNT-1,Red paint,Stock,Paint,quart,12.99,7.2,,Weighted Average,10,7.2000,2026-10-01
SO-1,Special order,non-stock,,,25,0,,,,,
NEW-1,No opening,stock,fasteners,each,1,1,,,,,
"""

BAD = HEAD + """
OK-1,Fine,stock,,each,1,1,,,5,1.00,2026-10-01
BAD 2,Bad sku,stock,,each,1,1,,,,,
OK-3,Fine,stock,,each,1,1,,,,,
OK-4,Bad type,widget,,each,1,1,,,,,
OK-5,Partial opening,stock,,each,1,1,,,5,,
"""


def test_valid_file_imports_items_and_opening_stock(admin_user, inv_settings):
    p = parse_items_csv(GOOD)
    assert p.errors == [] and len(p.rows) == 4
    items = apply_import(p, user=admin_user)
    assert [i.sku for i in items] == ["BOLT-1", "PNT-1", "SO-1", "NEW-1"]
    bolt = Item.objects.get(sku="BOLT-1")
    assert (bolt.costing_method, bolt.category.name, bolt.vendor_part_number) == (CostingMethod.FIFO, "Fasteners", "HB-1")
    assert Item.objects.get(sku="PNT-1").costing_method == CostingMethod.WEIGHTED_AVERAGE
    so = Item.objects.get(sku="SO-1")
    assert so.item_type == ItemType.NON_STOCK and so.inventory_account is None and so.costing_method == ""
    assert Item.objects.get(sku="NEW-1").category == bolt.category  # case-insensitive match, no duplicate category
    assert Category.objects.count() == 2
    assert StockMovement.objects.filter(kind=MovementKind.OPENING).count() == 2
    assert (on_hand(bolt), stock_value(bolt)) == (100, D("50.00"))
    assert check_invariants() == []


def test_any_bad_row_saves_nothing_and_lists_every_error(admin_user, inv_settings):
    p = parse_items_csv(BAD)
    assert [(e.row, e.message) for e in p.errors] == [
        (3, "Row 3: SKU can use letters, numbers, dots, dashes, and underscores (up to 50)."),
        (5, "Row 5: item_type must be stock or non-stock."),
        (6, "Row 6: opening_quantity, opening_unit_cost, and opening_date go together."),
    ]
    with pytest.raises(ValueError):
        apply_import(p, user=admin_user)
    assert Item.objects.count() == 0 and StockMovement.objects.count() == 0


def test_case_insensitive_duplicate_sku_in_file(inv_settings):  # Review Focus 4
    p = parse_items_csv(HEAD + "\nBOLT-1,A,stock,,,0,0,,,,,\nbolt-1,B,stock,,,0,0,,,,,\n")
    assert [e.message for e in p.errors] == ["Row 3: SKU bolt-1 appears more than once in this file."]


def test_sku_already_in_database_any_case(inv_settings, make_item):
    make_item("BOLT-1")
    p = parse_items_csv(HEAD + "\nbolt-1,A,stock,,,0,0,,,,,\n")
    assert [e.message for e in p.errors] == ["Row 2: SKU bolt-1 already exists."]


def test_bom_and_mixed_case_values(inv_settings):
    p = parse_items_csv("﻿" + HEAD.upper() + "\nA-1,A,STOCK,,,1,1,,FIFO,,,\n")
    assert p.errors == [] and p.rows[0].costing_method == CostingMethod.FIFO


def test_missing_columns_reported_once_as_row_one(inv_settings):
    p = parse_items_csv("sku,name\nA-1,A\n")
    assert len(p.errors) == 1 and p.errors[0].row == 1
    assert p.errors[0].message.startswith("Row 1: missing columns: item_type, category, unit,")


def test_non_stock_cannot_have_method_or_opening(inv_settings):
    p = parse_items_csv(HEAD + "\nA-1,A,non-stock,,,1,1,,fifo,,,\nA-2,A,non-stock,,,1,1,,,5,1.00,2026-10-01\n")
    assert [e.message for e in p.errors] == [
        "Row 2: non-stock items can't have a costing method or opening stock.",
        "Row 3: non-stock items can't have a costing method or opening stock.",
    ]


@pytest.mark.parametrize("cell,message", [
    ("sale_price", "Row 2: sale_price must be a number with at most 4 decimal places."),
    ("purchase_cost", "Row 2: purchase_cost must be a number with at most 4 decimal places."),
])
def test_price_columns_validated(cell, message, inv_settings):
    row = {"sku": "A-1", "name": "A", "item_type": "stock", "sale_price": "1", "purchase_cost": "1"}
    row[cell] = "1.23456"
    p = parse_items_csv(HEAD + "\n" + ",".join(row.get(h, "") for h in HEADERS) + "\n")
    assert [e.message for e in p.errors] == [message]


def test_opening_field_rules(inv_settings, admin_user):
    close_period(2026, 9, admin_user)
    rows = [
        "A-1,A,stock,,,1,1,,,0,1.00,2026-10-01",
        "A-2,A,stock,,,1,1,,,2.5,1.00,2026-10-01",
        "A-3,A,stock,,,1,1,,,5,1.00,10/01/2026",
        "A-4,A,stock,,,1,1,,,5,1.00,2026-09-15",
        "A-5,A,stock,,,1,1,,,5,-1,2026-10-01",
    ]
    p = parse_items_csv(HEAD + "\n" + "\n".join(rows) + "\n")
    assert [e.message for e in p.errors] == [
        "Row 2: opening_quantity must be a whole number above zero.",
        "Row 3: opening_quantity must be a whole number above zero.",
        "Row 4: opening_date must look like 2026-10-31.",
        "Row 5: opening_date is in a closed month.",
        "Row 6: opening_unit_cost must be a number with at most 4 decimal places.",
    ]


def test_apply_without_settings_saves_nothing(admin_user, db, revenue):  # Review Focus 1
    p = parse_items_csv(GOOD)
    with pytest.raises(SettingsMissingError, match="Set up inventory accounts first."):
        apply_import(p, user=admin_user)
    assert Item.objects.count() == 0


def test_apply_is_admin_only(standard_user, inv_settings):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        apply_import(parse_items_csv(GOOD), user=standard_user)


def test_blank_rows_skipped(inv_settings):
    p = parse_items_csv(HEAD + "\n\nA-1,A,stock,,,1,1,,,,,\n,,,,,,,,,,,\n")
    assert p.errors == [] and len(p.rows) == 1

"""CSV item import. parse_items_csv validates every row; apply_import saves all rows or none."""
import csv
import io
import re
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models.functions import Lower

from core.roles import require_admin
from ledger.periods import is_closed

from .errors import SettingsMissingError
from .models import SKU_MESSAGE, SKU_PATTERN, Category, CostingMethod, InventorySettings, Item, ItemType, MovementKind

HEADERS = [
    "sku", "name", "item_type", "category", "unit", "sale_price", "purchase_cost", "vendor_part_number",
    "costing_method", "opening_quantity", "opening_unit_cost", "opening_date",
]
ITEM_TYPES = {"stock": ItemType.STOCK, "non-stock": ItemType.NON_STOCK}
METHODS = {"fifo": CostingMethod.FIFO, "weighted average": CostingMethod.WEIGHTED_AVERAGE}


@dataclass(frozen=True)
class RowError:
    row: int
    message: str


@dataclass(frozen=True)
class ParsedRow:
    row: int
    sku: str
    name: str
    item_type: ItemType
    category: str
    unit: str
    sale_price: Decimal
    purchase_cost: Decimal
    vendor_part_number: str
    costing_method: CostingMethod | None
    opening_quantity: int | None
    opening_unit_cost: Decimal | None
    opening_date: date_type | None


@dataclass(frozen=True)
class ImportPreview:
    rows: list[ParsedRow]
    errors: list[RowError]


def _price(value: str) -> Decimal | None:
    """A non-negative number with at most 4 decimal places, or None if it isn't one."""
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or number.as_tuple().exponent < -4:
        return None
    return number


def parse_items_csv(text: str) -> ImportPreview:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [h for h in HEADERS if h not in reader.fieldnames]
    if missing:
        return ImportPreview([], [RowError(1, f"Row 1: missing columns: {', '.join(missing)}.")])

    existing = set(Item.objects.annotate(k=Lower("sku")).values_list("k", flat=True))
    seen: set[str] = set()
    rows: list[ParsedRow] = []
    errors: list[RowError] = []

    for n, raw in enumerate(reader, start=2):
        cells = {h: (raw.get(h) or "").strip() for h in HEADERS}
        if not any(cells.values()):
            continue
        problems: list[str] = []

        sku = cells["sku"]
        if not sku:
            problems.append("SKU is required.")
        elif not re.match(SKU_PATTERN, sku):
            problems.append(SKU_MESSAGE)
        elif sku.lower() in existing:
            problems.append(f"SKU {sku} already exists.")
        elif sku.lower() in seen:
            problems.append(f"SKU {sku} appears more than once in this file.")
        if sku:
            seen.add(sku.lower())

        if not cells["name"]:
            problems.append("name is required.")

        item_type = ITEM_TYPES.get(cells["item_type"].lower())
        if item_type is None:
            problems.append("item_type must be stock or non-stock.")

        method = None
        if cells["costing_method"]:
            method = METHODS.get(cells["costing_method"].lower().replace("_", " "))
            if method is None:
                problems.append("costing_method must be fifo or weighted average.")

        prices = {}
        for column in ("sale_price", "purchase_cost"):
            prices[column] = _price(cells[column] or "0")
            if prices[column] is None:
                problems.append(f"{column} must be a number with at most 4 decimal places.")

        opening_cells = [cells["opening_quantity"], cells["opening_unit_cost"], cells["opening_date"]]
        has_opening = any(opening_cells)
        quantity = unit_cost = opening_date = None
        if item_type == ItemType.NON_STOCK:
            if cells["costing_method"] or has_opening:
                problems.append("non-stock items can't have a costing method or opening stock.")
        elif item_type == ItemType.STOCK:
            method = method or (CostingMethod.FIFO if not cells["costing_method"] else None)
            if has_opening and not all(opening_cells):
                problems.append("opening_quantity, opening_unit_cost, and opening_date go together.")
            elif has_opening:
                if re.fullmatch(r"\d+", cells["opening_quantity"]) and int(cells["opening_quantity"]) > 0:
                    quantity = int(cells["opening_quantity"])
                else:
                    problems.append("opening_quantity must be a whole number above zero.")
                unit_cost = _price(cells["opening_unit_cost"])
                if unit_cost is None:
                    problems.append("opening_unit_cost must be a number with at most 4 decimal places.")
                try:
                    opening_date = date_type.fromisoformat(cells["opening_date"])
                except ValueError:
                    problems.append("opening_date must look like 2026-10-31.")
                else:
                    if is_closed(opening_date):
                        problems.append("opening_date is in a closed month.")

        if problems:
            errors.extend(RowError(n, f"Row {n}: {p if p.endswith('.') else p + '.'}") for p in problems)
            continue
        rows.append(ParsedRow(
            row=n, sku=sku, name=cells["name"], item_type=item_type, category=cells["category"],
            unit=cells["unit"] or "each", sale_price=prices["sale_price"], purchase_cost=prices["purchase_cost"],
            vendor_part_number=cells["vendor_part_number"],
            costing_method=method if item_type == ItemType.STOCK else None,
            opening_quantity=quantity, opening_unit_cost=unit_cost, opening_date=opening_date))
    return ImportPreview(rows, errors)


def apply_import(preview: ImportPreview, *, user) -> list[Item]:
    from .stock import StockLine, record_movements  # stock imports models; avoid a cycle at import time

    require_admin(user)
    if preview.errors:
        raise ValueError("The file has errors. Fix them and upload again.")
    with transaction.atomic():
        s = InventorySettings.load()
        if not s.is_configured:
            raise SettingsMissingError("Set up inventory accounts first.")
        categories = {c.name.lower(): c for c in Category.objects.all()}
        items: list[Item] = []
        openings: dict[date_type, list[StockLine]] = {}
        for r in preview.rows:
            category = None
            if r.category:
                category = categories.get(r.category.lower())
                if category is None:
                    category = categories[r.category.lower()] = Category.objects.create(name=r.category)
            stock = r.item_type == ItemType.STOCK
            item = Item.objects.create(
                sku=r.sku, name=r.name, item_type=r.item_type, category=category, unit=r.unit,
                sale_price=r.sale_price, purchase_cost=r.purchase_cost, vendor_part_number=r.vendor_part_number,
                costing_method=r.costing_method if stock else "", income_account=s.default_income_account,
                cogs_account=s.default_cogs_account if stock else None,
                inventory_account=s.default_inventory_account if stock else None)
            items.append(item)
            if r.opening_quantity:
                openings.setdefault(r.opening_date, []).append(StockLine(item, r.opening_quantity, unit_cost=r.opening_unit_cost))
        for day, lines in sorted(openings.items()):
            record_movements(kind=MovementKind.OPENING, date=day, lines=lines, user=user)
        return items

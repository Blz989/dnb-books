from decimal import Decimal

import pytest
from django.core.management import call_command

from items.models import Item
from migration.zoho.reader import REQUIRED, OPTIONAL, read_export


def _items_csv(tmp_path, extra_header="", extra_cell=""):
    cols = list(REQUIRED["items"])
    header = ["Item ID", *cols] + ([extra_header] if extra_header else [])
    row = {c: "" for c in header}
    row.update({"Item ID": "1", "Item Name": "Rice stick", "SKU": "RICE-1", "Item Type": "inventory", "Status": "Active",
                "Category Name": "Dry Goods", "Usage unit": "CASE", "Rate": "80", "Purchase Rate": "50"})
    if extra_header:
        row[extra_header] = extra_cell
    for short, name in (("accounts", "Chart_of_Accounts.csv"), ("customers", "Contacts.csv"), ("vendors", "Vendors.csv")):
        from migration.zoho.reader import ID_COLUMNS
        head = [c for c in (ID_COLUMNS[short], *REQUIRED[short]) if c]
        (tmp_path / name).write_text(",".join(head) + "\n")
    (tmp_path / "Item.csv").write_text(",".join(header) + "\n" + ",".join(row[c] for c in header) + "\n")
    return tmp_path


def test_reader_keeps_optional_packaging_when_present(tmp_path):
    export = read_export(_items_csv(tmp_path, "CF.Packaging", "25X16OZ"))
    assert export.files["items"][0]["CF.Packaging"] == "25X16OZ"


def test_reader_does_not_require_packaging(tmp_path):
    export = read_export(_items_csv(tmp_path))
    assert export.files["items"][0].get("CF.Packaging", "") == ""
    assert "CF.Packaging" in OPTIONAL["items"]


def test_backfill_command_sets_packaging_by_sku(tmp_path, make_item, capsys):
    make_item("RICE-1")
    make_item("OTHER-1", packaging="keep")
    path = _items_csv(tmp_path, "CF.Packaging", "25X16OZ") / "Item.csv"
    call_command("backfill_packaging", str(path))
    assert Item.objects.get(sku="RICE-1").packaging == "25X16OZ"
    assert Item.objects.get(sku="OTHER-1").packaging == "keep"
    assert "1 item" in capsys.readouterr().out

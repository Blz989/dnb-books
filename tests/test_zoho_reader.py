import zipfile

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction

from migration.models import ZohoRecord
from migration.zoho.reader import REQUIRED, ExportError, read_export
from tests.zoho_sample import build_sample_export, sample_rows, zip_export


def test_reads_nested_zips_in_name_order(tmp_path):
    folder = build_sample_export(tmp_path)
    with zipfile.ZipFile(folder / "Invoice.zip") as z:
        inner = {n: z.read(n).decode("utf-8-sig").count("\r\n") - 1 for n in z.namelist()}
    assert len(inner) == 2 and all(n > 0 for n in inner.values())
    export = read_export(folder)
    assert len(export.files["invoices"]) == sum(inner.values()) == len(sample_rows()["invoices"])
    numbers = [r["Invoice Number"] for r in export.files["invoices"]]
    assert numbers[0] == "INV-000001" and numbers[-1] == "INV-000009"
    assert numbers == sorted(numbers)
    assert len(export.files["sales_orders"]) == len(sample_rows()["sales_orders"])


def test_reads_a_single_outer_zip(tmp_path):
    folder = build_sample_export(tmp_path)
    export = read_export(zip_export(folder))
    assert len(export.files["bills"]) == len(sample_rows()["bills"])
    assert len(export.files["invoices"]) == len(sample_rows()["invoices"])


def test_strips_bom_and_reads_quoted_commas(tmp_path):
    folder = build_sample_export(tmp_path)
    assert (folder / "Item.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    export = read_export(folder)
    assert "Item ID" in export.files["items"][0]
    descs = {r["Item Desc"] for r in export.files["invoices"]}
    assert "Box, 12 ct" in descs
    assert any(r["Description"] == "Box, 12 ct" for r in export.files["items"])
    assert any(r["Total"] == "1,012.50" for r in export.files["invoices"])


def test_only_required_columns_and_row_ids(tmp_path):
    export = read_export(build_sample_export(tmp_path))
    row = export.files["transfers"][0]
    assert set(row) == set(REQUIRED["transfers"]) | {"_row", "_id"}
    assert [r["_id"] for r in export.files["transfers"]] == [
        f"Transfer_Fund.csv:{n}" for n in (1, 2, 3)
    ]
    assert "_id" not in export.files["bills"][0]


def test_missing_column_reports_file_and_column(tmp_path):
    folder = build_sample_export(tmp_path, drop_column=("bills", "Rate"))
    with pytest.raises(ExportError) as e:
        read_export(folder)
    assert e.value.problems == ['Bill.csv: column "Rate" is missing.']


def test_missing_file_is_empty_list_except_core(tmp_path):
    folder = build_sample_export(tmp_path)
    (folder / "Vendor_Credits.csv").unlink()
    assert read_export(folder).files["vendor_credits"] == []
    (folder / "Chart_of_Accounts.csv").unlink()
    with pytest.raises(ExportError) as e:
        read_export(folder)
    assert e.value.problems == ['Chart_of_Accounts.csv: column "Account Name" is missing.']


def test_extra_rows_are_appended(tmp_path):
    extra = {"customers": [{"Contact ID": "1", "Display Name": "Zed Co"}]}
    export = read_export(build_sample_export(tmp_path, extra_rows=extra))
    assert export.files["customers"][-1]["Display Name"] == "Zed Co"
    assert export.files["customers"][-1]["Phone"] == ""


def test_sample_has_core_and_listed_accounts(tmp_path):
    names = {r["Account Name"] for r in read_export(build_sample_export(tmp_path)).files["accounts"]}
    assert {"Accounts Receivable", "Accounts Payable", "Undeposited Funds", "Petty Cash",
            "Inventory Asset", "Cost of Goods Sold", "Sales", "Shipping Charge", "Tax Payable",
            "Bank Fees and Charges", "Opening Balance Offset", "BUS COMPLETE CHK",
            "Chase Ink Unlimited", "Line Of Credit - Chase", "Line Of Credit - Headway",
            "Shipping Expenses"} <= names


@pytest.mark.django_db
def test_models_unique_zoho_record(admin_user):
    ct = ContentType.objects.get_for_model(type(admin_user))
    ZohoRecord.objects.create(kind="customer", zoho_id="Z1", content_type=ct, object_id=admin_user.pk)
    ZohoRecord.objects.create(kind="vendor", zoho_id="Z1", content_type=ct, object_id=admin_user.pk)
    with pytest.raises(IntegrityError), transaction.atomic():
        ZohoRecord.objects.create(kind="customer", zoho_id="Z1", content_type=ct, object_id=1)

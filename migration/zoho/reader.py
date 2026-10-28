"""Reads a Zoho Books export (a folder or a zip) into plain row dicts.

Only the columns in REQUIRED (plus each file's ID column) are kept. Every row
also gets "_row" (1-based data row number within its short name, across the
joined inner files) and, for files with no ID column, "_id" = "{file}:{row}".
"""
import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

csv.field_size_limit(10**9)

# short name -> file. A ".zip" holds several inner CSVs, read in name order and joined.
FILES: dict[str, str] = {
    "accounts": "Chart_of_Accounts.csv",
    "items": "Item.csv",
    "customers": "Contacts.csv",
    "vendors": "Vendors.csv",
    "sales_orders": "Sales_Order.zip",
    "invoices": "Invoice.zip",
    "purchase_orders": "Purchase_Order.csv",
    "bills": "Bill.csv",
    "customer_payments": "Customer_Payment.csv",
    "vendor_payments": "Vendor_Payment.csv",
    "credit_notes": "Credit_Note.csv",
    "credit_applications": "Creditnotes_Invoice.csv",
    "vendor_credits": "Vendor_Credits.csv",
    "expenses": "Expense.csv",
    "journals": "Journal.csv",
    "deposits": "Deposit.csv",
    "transfers": "Transfer_Fund.csv",
    "adjustments": "Inventory_Adjustment.csv",
}

# short name -> ID column ("" when the file has none).
ID_COLUMNS: dict[str, str] = {
    "accounts": "Account ID",
    "items": "Item ID",
    "customers": "Contact ID",
    "vendors": "Contact ID",
    "sales_orders": "SalesOrder ID",
    "invoices": "Invoice ID",
    "purchase_orders": "Purchase Order ID",
    "bills": "Bill ID",
    "customer_payments": "CustomerPayment ID",
    "vendor_payments": "VendorPayment ID",
    "credit_notes": "CreditNotes ID",
    "credit_applications": "",
    "vendor_credits": "Vendor Credit ID",
    "expenses": "Expense Reference ID",
    "journals": "Journal ID",
    "deposits": "",
    "transfers": "",
    "adjustments": "InventoryAdjustment ID",
}

# short name -> the columns read (the ID column is read in addition).
REQUIRED: dict[str, tuple[str, ...]] = {
    "accounts": ("Account Name", "Account Type", "Account Status", "Description"),
    "items": (
        "Item Name", "SKU", "Description", "Rate", "Purchase Rate", "Account",
        "Purchase Account", "Inventory Account", "Item Type", "Status", "Usage unit",
        "Category Name", "Vendor", "CF.Vendor SKU", "Opening Stock", "Opening Stock Value",
    ),
    "customers": (
        "Display Name", "Company Name", "EmailID", "Phone", "Billing Address",
        "Billing Street2", "Billing City", "Billing State", "Billing Code",
        "Shipping Address", "Shipping Street2", "Shipping City", "Shipping State",
        "Shipping Code", "Payment Terms Label", "Status", "Notes",
    ),
    "vendors": (
        "Display Name", "EmailID", "Phone", "Billing Address", "Billing City",
        "Billing State", "Billing Code", "Payment Terms Label", "Status",
    ),
    "sales_orders": (
        "Order Date", "SalesOrder Number", "Status", "Customer Name", "Item Name", "SKU",
        "Item Desc", "QuantityOrdered", "Item Price", "Item Total", "Total",
        "Shipping Charge", "Adjustment", "Notes",
    ),
    "invoices": (
        "Invoice Date", "Invoice Number", "Invoice Status", "Customer Name",
        "Sales Order Number", "Item Name", "SKU", "Item Desc", "Quantity", "Item Price",
        "Item Total", "Total", "Shipping Charge", "Shipping Charge Account", "Adjustment",
        "Adjustment Account", "Item Tax Amount", "Notes", "Payment Terms Label",
     "Due Date",
    ),
    "purchase_orders": (
        "Purchase Order Date", "Purchase Order Number", "Vendor Name", "Item Name", "SKU",
        "QuantityOrdered", "Item Price", "Item Total",
    ),
    "bills": (
        "Bill Date", "Bill Number", "Vendor Name", "Bill Status", "Item Name", "SKU",
        "Account", "Description", "Quantity", "Rate", "Item Total", "Total", "Adjustment",
        "Adjustment Account", "Payment Terms Label", "Purchase Order Number",
        "CF.Sales Order",
     "Balance",
     "Due Date",
    ),
    "customer_payments": (
        "Payment Number", "Mode", "Date", "Amount", "Unused Amount", "Bank Charges",
        "Reference Number", "Customer Name", "Deposit To", "Invoice Number",
        "Amount Applied to Invoice", "Invoice Payment Applied Date",
    ),
    "vendor_payments": (
        "Payment Number", "Mode", "Date", "Amount", "Reference Number", "Vendor Name",
        "Paid Through", "Bill Number", "Bill Amount", "Bill Payment Applied Date",
    ),
    "credit_notes": (
        "Credit Note Date", "Credit Note Number", "Customer Name", "Item Name", "Account",
        "Item Total", "Total", "Subject", "Notes",
    ),
    "credit_applications": ("Credit Note Number", "Invoice Number", "Amount", "Date"),
    "vendor_credits": (
        "Vendor Credit Date", "Vendor Credit Number", "Vendor Name", "Item Name", "SKU",
        "Quantity", "Account", "Item Total", "Total", "Balance",
    ),
    "expenses": (
        "Expense Date", "Expense Description", "Expense Account", "Paid Through",
        "Expense Amount", "Total", "Reference#", "Vendor",
    ),
    "journals": (
        "Journal Date", "Journal Number", "Notes", "Account", "Debit", "Credit",
        "Description",
    ),
    "deposits": (
        "Transaction Date", "Transaction Type", "From Account", "To Account", "Reference",
        "Description", "Total",
    ),
    "transfers": (
        "Transaction Date", "Transaction Type", "From Account", "To Account", "Reference",
        "Description", "Total",
    ),
    "adjustments": (
        "Date", "Adjustment Type", "Reason", "Description", "Item Name", "SKU", "Account",
        "Quantity Adjusted", "Value Adjusted",
    ),
}

# A missing file is an empty list, except for these: the importer cannot start without them.
CORE = ("accounts", "items", "customers", "vendors")


class ExportError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class ZohoExport:
    files: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # short name -> [(file name, data rows in it)] in read order; lets row numbers be file lines.
    origins: dict[str, list[tuple[str, int]]] = field(default_factory=dict)


class _Source:
    """A folder, or a zip of the same files (possibly under one top folder)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def read(self, name: str) -> bytes | None:
        if self._zip is None:
            f = self.path / name
            return f.read_bytes() if f.is_file() else None
        for member in self._zip.namelist():
            if member.rsplit("/", 1)[-1] == name:
                return self._zip.read(member)
        return None

    def close(self):
        if self._zip:
            self._zip.close()


def _inner_csvs(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(n for n in z.namelist() if n.lower().endswith(".csv"))
        return [(n.rsplit("/", 1)[-1], z.read(n)) for n in names]


def _parse(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return list(reader.fieldnames or []), list(reader)


def read_export(path: Path) -> ZohoExport:
    problems: list[str] = []
    result: dict[str, list[dict[str, str]]] = {}
    origins: dict[str, list[tuple[str, int]]] = {}
    source = _Source(path)
    try:
        for short, filename in FILES.items():
            wanted = tuple(c for c in (ID_COLUMNS[short], *REQUIRED[short]) if c)
            data = source.read(filename)
            if data is None:
                if short in CORE:
                    problems.append(f'{filename}: column "{REQUIRED[short][0]}" is missing.')
                result[short] = []
                continue
            parts = (
                _inner_csvs(data) if filename.endswith(".zip") else [(filename, data)]
            )
            rows: list[dict[str, str]] = []
            missing: set[str] = set()
            for inner_name, blob in parts:
                header, part_rows = _parse(blob)
                for col in wanted:
                    if col not in header and col not in missing:
                        missing.add(col)
                        problems.append(f'{inner_name}: column "{col}" is missing.')
                rows.extend(part_rows)
                origins.setdefault(short, []).append((inner_name, len(part_rows)))
            if missing:
                result[short] = []
                continue
            out = []
            for n, row in enumerate(rows, start=1):
                clean = {col: (row.get(col) or "") for col in wanted}
                clean["_row"] = str(n)
                if not ID_COLUMNS[short]:
                    clean["_id"] = f"{filename}:{n}"
                out.append(clean)
            result[short] = out
    finally:
        source.close()
    if problems:
        raise ExportError(problems)
    return ZohoExport(files=result, origins=origins)

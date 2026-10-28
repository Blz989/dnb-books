"""Checks a read Zoho export for bad references before anything is imported.

Every problem is a plain sentence naming the file and row (file line number,
header = 1), so the owner can fix the export and run it again.
"""
from migration.zoho import mapping
from migration.zoho.reader import FILES, ID_COLUMNS, ZohoExport

CUSTOMER_SHORTS = ("sales_orders", "invoices", "customer_payments", "credit_notes")
VENDOR_SHORTS = ("bills", "purchase_orders", "vendor_payments", "vendor_credits")
CUSTOMER_COLUMN = {s: "Customer Name" for s in CUSTOMER_SHORTS}
VENDOR_COLUMN = {s: "Vendor Name" for s in VENDOR_SHORTS}
# Credit notes post to accounts in DNB Books, so their item names are only descriptions.
ITEM_LINES = ("sales_orders", "invoices", "purchase_orders", "bills", "vendor_credits")
ACCOUNT_COLUMNS = {
    "invoices": ("Shipping Charge Account", "Adjustment Account"),
    "bills": ("Account", "Adjustment Account"),
    "credit_notes": ("Account",),
    "vendor_credits": ("Account",),
    "customer_payments": ("Deposit To",),
    "vendor_payments": ("Paid Through",),
    "expenses": ("Expense Account", "Paid Through"),
    "journals": ("Account",),
    "deposits": ("From Account", "To Account"),
    "transfers": ("From Account", "To Account"),
    "adjustments": ("Account",),
}
# short -> number column
UNIQUE_NUMBERS = {
    "sales_orders": "SalesOrder Number",
    "invoices": "Invoice Number",
    "customer_payments": "Payment Number",
    "vendor_payments": "Payment Number",
    "credit_notes": "Credit Note Number",
    "purchase_orders": "Purchase Order Number",
}
TERMS_SHORTS = ("customers", "vendors", "invoices", "bills")


def _where(export: ZohoExport, short: str, row: dict) -> str:
    """'{file} row {n}' where n is the line in the file (header = 1)."""
    n = int(row["_row"])
    for name, count in export.origins.get(short, []):
        if n <= count:
            return f"{name} row {n + 1}"
        n -= count
    return f"{FILES[short]} row {int(row['_row']) + 1}"


def validate(export: ZohoExport) -> list[str]:
    files = export.files
    problems: list[str] = []
    seen: set[tuple] = set()

    def report(short: str, row: dict, text: str) -> None:
        # One problem per document, not one per line of the same document.
        doc = row.get(ID_COLUMNS[short]) or row.get("_id") or row["_row"]
        key = (short, doc, text)
        if key not in seen:
            seen.add(key)
            problems.append(f"{_where(export, short, row)}: {text}")

    def rows(short: str):
        return files.get(short, [])

    # Accounts the mapping cannot place.
    supported = []
    for row in rows("accounts"):
        ztype = row["Account Type"].strip()
        if ztype in mapping.ACCOUNT_RULES:
            supported.append(row)
        else:
            report("accounts", row, f'account type "{ztype}" is not supported.')
    try:
        mapping.account_plan(supported)
    except ValueError as exc:
        problems.append(f"{FILES['accounts']}: {exc}")

    # Payment terms the mapping rejects.
    for short in TERMS_SHORTS:
        for row in rows(short):
            label = row["Payment Terms Label"]
            try:
                mapping.terms(label)
            except ValueError:
                report(short, row, f'payment terms "{label.strip()}" is not supported.')

    customers = {r["Display Name"] for r in rows("customers")}
    customer_ids = {r["Contact ID"] for r in rows("customers")}
    vendors = {r["Display Name"] for r in rows("vendors")}
    accounts = {r["Account Name"] for r in rows("accounts")}
    skus = {r["SKU"] for r in rows("items") if r["SKU"]}
    names = {r["Item Name"] for r in rows("items")}
    invoices = {r["Invoice Number"] for r in rows("invoices")}
    sales_orders = {r["SalesOrder Number"] for r in rows("sales_orders")}
    bill_vendors: dict[str, set[str]] = {}
    for r in rows("bills"):
        bill_vendors.setdefault(r["Bill Number"], set()).add(r["Vendor Name"])

    def missing(kind: str, value: str) -> str:
        return f'{kind} "{value}" is not in the export.'

    for short, col in CUSTOMER_COLUMN.items():
        id_col = mapping.CUSTOMER_ID_COLUMN[short]
        for row in rows(short):
            contact_id = row.get(id_col, "").strip()
            if contact_id:
                if contact_id not in customer_ids:
                    report(short, row, missing("customer", f"{row[col]} ({contact_id})"))
            elif row[col] and row[col] not in customers:
                report(short, row, missing("customer", row[col]))
    for short, col in VENDOR_COLUMN.items():
        for row in rows(short):
            if row[col] and row[col] not in vendors:
                report(short, row, missing("vendor", row[col]))

    for short in ITEM_LINES:
        for row in rows(short):
            sku, name = row.get("SKU", ""), row["Item Name"]
            if sku:
                if sku not in skus:
                    report(short, row, missing("item", sku))
            elif name and name not in names and not (short == "bills" and row["Account"]):
                report(short, row, missing("item", name))

    for short, cols in ACCOUNT_COLUMNS.items():
        for row in rows(short):
            for col in cols:
                if row[col] and row[col] not in accounts:
                    report(short, row, missing("account", row[col]))

    for short in ("customer_payments", "credit_applications"):
        for row in rows(short):
            if row["Invoice Number"] and row["Invoice Number"] not in invoices:
                report(short, row, missing("invoice", row["Invoice Number"]))
    for row in rows("invoices"):
        so = row["Sales Order Number"]
        if so and so not in sales_orders:
            report("invoices", row, missing("sales order", so))

    # Zoho's vendor credit export has no bill column; its applications are rebuilt from bill balances.
    for short in ("vendor_payments",):
        for row in rows(short):
            bill, vendor = row["Bill Number"], row["Vendor Name"]
            if not bill:
                continue
            if bill not in bill_vendors:
                report(short, row, missing("bill", bill))
            elif vendor not in bill_vendors[bill]:
                report(short, row, f'bill "{bill}" is not a bill of vendor "{vendor}".')

    for short, col in UNIQUE_NUMBERS.items():
        owner: dict[str, str] = {}
        for row in rows(short):
            number, doc = row[col], row[ID_COLUMNS[short]]
            if not number:
                continue
            if owner.setdefault(number, doc) != doc:
                report(short, row, f"number {number} appears twice.")

    return problems

"""A made-up Zoho Books export, shaped like the real one, for the migration tests.

Structure
- `sample_rows()` returns `{short_name: [row dict, ...]}` (fresh copy each call). Keys are
  the real Zoho column names; a short name's keys are its ID column plus
  `migration.zoho.reader.REQUIRED[short]`. Missing keys are written as "".
- `build_sample_export(tmp_path, ...)` writes those rows as the export folder
  (`tmp_path / "zoho_export"`): top-level CSVs (utf-8-sig), plus `Invoice.zip` and
  `Sales_Order.zip`, each holding two inner CSVs (`Invoice00.csv`, `Invoice01.csv`).
  The inner files are added to the zip in reverse name order on purpose.
- `zip_export(folder)` zips that folder into one outer zip, like the real download.
- Module constants name the key facts so tests need no magic numbers (see EXPECTED).

Document totals are computed from the lines by the `_invoice`, `_bill` ... helpers, so
they always agree with the line amounts. Every invoice is paid or open as its payment
rows say; the story is in the comments next to each document.
"""
import csv
import io
import shutil
import zipfile
from decimal import Decimal as D
from pathlib import Path

from migration.zoho.reader import FILES, ID_COLUMNS, REQUIRED

BANK = "BUS COMPLETE CHK"
CARD = "Chase Ink Unlimited"
LOC_CHASE = "Line Of Credit - Chase"
LOC_HEADWAY = "Line Of Credit - Headway"
UNDEPOSITED = "Undeposited Funds"

# Zoho splits big document files into numbered inner CSVs. (short name -> lines of docs per file)
ZIP_SPLIT_AFTER = {"invoices": 4, "sales_orders": 4}  # documents in the first inner file

# Items: key -> (name, sku, description, rate, purchase rate, opening stock, type)
ITEMS = {
    "A": ("Blue Widget", "WID-A", "Blue widget", D("25.00"), D("12.00"), None, "Inventory"),
    "B": ("Red Widget", "WID-B", "Red widget", D("30.00"), D("15.00"), None, "Inventory"),
    "C": ("Gadget", "GAD-C", "Gadget, large", D("50.00"), D("28.00"), (40, D("1000.00")), "Inventory"),
    "D": ("Hex Bolt Box", "BOLT-D", "Box, 12 ct", D("8.00"), D("3.00"), None, "Inventory"),
    "W": ("Gift Wrap", "WRAP", "Gift wrapping", D("5.00"), D("0.00"), None, "Service"),
}

CUSTOMERS = ["Acme Hardware", "Birch Lane Supply", "Cedar Ridge Outfitters"]
VENDORS = ["Mr. Joseph Cordon", "Pioneer Wholesale", "Summit Packaging"]

# Facts the later tasks' tests can assert against.
EXPECTED = {
    "invoice_totals": {  # Zoho "Total" per invoice number
        "INV-000001": D("160.00"), "INV-000002": D("1012.50"), "INV-000003": D("1000.00"),
        "INV-000004": D("625.00"), "INV-000005": D("375.00"), "INV-000006": D("599.50"),
        "INV-000007": D("295.00"), "INV-000008": D("321.00"), "INV-000009": D("510.00"),
    },
    "bill_totals": {
        "BILL-001": D("1950.00"), "BILL-002": D("1140.00"), "BILL-003": D("720.00"),
        "BILL-004": D("150.00"), "BILL-005": D("45.00"),
    },
    "negative_item_sku": "BOLT-D",  # invoiced 2024-03-02, first received 2024-08-01
    "undeposited_transfer": D("535.00"),  # exactly cash payments "1" (160.00) + "3" (375.00)
}


def _m(value) -> str:
    """Plain Zoho money text. Amounts of 1,000 or more are written with a comma,
    as some Zoho exports do, and quoted by the CSV writer."""
    if value is None or value == "":
        return ""
    d = D(value)
    return f"{d:,.2f}" if abs(d) >= 1000 and d == d.quantize(D("0.01")) else f"{d:.2f}"


def _q(value) -> str:
    return "" if value is None else str(value)


def _line_total(qty, price, item_total):
    return D(item_total) if item_total is not None else D(qty) * D(price)


def _item(key):
    return ITEMS[key]


# ---------------------------------------------------------------- accounts / master data
def _accounts():
    core = [
        ("Accounts Receivable", "Accounts Receivable"),
        ("Accounts Payable", "Accounts Payable"),
        (UNDEPOSITED, "Other Current Asset"),
        ("Petty Cash", "Cash"),
        ("Inventory Asset", "Stock"),
        ("Cost of Goods Sold", "Cost Of Goods Sold"),
        ("Sales", "Income"),
        ("Discount", "Income"),
        ("Other Charges", "Other Income"),
        ("Tax Payable", "Other Current Liability"),
        ("Bank Fees and Charges", "Expense"),
        ("Office Supplies", "Expense"),
        ("Interest Expense", "Expense"),
        ("Opening Balance Offset", "Equity"),
        ("Owner's Equity", "Equity"),
        ("Retained Earnings", "Equity"),
    ]
    extra = [
        (BANK, "Bank"),
        (CARD, "Credit Card"),
        (LOC_CHASE, "Credit Card"),
        (LOC_HEADWAY, "Other Liability"),
        ("Shipping Expenses", "Expense"),
        ("Shipping Charge", "Income"),
    ]
    return [
        {"Account ID": f"46000000{i:05d}", "Account Name": n, "Account Type": t,
         "Account Status": "Active", "Description": ""}
        for i, (n, t) in enumerate(core + extra, start=1)
    ]


def _items():
    rows = []
    for i, (key, (name, sku, desc, rate, prate, opening, kind)) in enumerate(ITEMS.items(), 1):
        stock = kind == "Inventory"
        rows.append({
            "Item ID": f"47000000{i:05d}", "Item Name": name, "SKU": sku, "Description": desc,
            "Rate": _m(rate), "Purchase Rate": _m(prate) if stock else "",
            "Account": "Sales", "Purchase Account": "Cost of Goods Sold" if stock else "",
            "Inventory Account": "Inventory Asset" if stock else "", "Item Type": kind,
            "Status": "Active", "Usage unit": "pcs" if stock else "",
            "Category Name": "Hardware" if stock else "Services",
            "Vendor": "Pioneer Wholesale" if stock else "", "CF.Vendor SKU": f"PW-{sku}" if stock else "",
            "Opening Stock": str(opening[0]) if opening else "",
            "Opening Stock Value": _m(opening[1]) if opening else "",
        })
    return rows


def _customers():
    base = [
        ("Acme Hardware", "Acme Hardware LLC", "ap@acme.example", "555-0101", "12 Main St", "Austin", "TX", "78701", "Net 30"),
        ("Birch Lane Supply", "Birch Lane Supply Co", "pay@birch.example", "555-0102", "9 Birch Ln", "Denver", "CO", "80202", "Net 15"),
        ("Cedar Ridge Outfitters", "Cedar Ridge Outfitters Inc", "bills@cedar.example", "555-0103", "300 Ridge Rd", "Boise", "ID", "83702", "Due on Receipt"),
    ]
    rows = []
    for i, (name, co, email, phone, street, city, st, zipc, terms) in enumerate(base, 1):
        rows.append({
            "Contact ID": f"48000000{i:05d}", "Display Name": name, "Company Name": co,
            "EmailID": email, "Phone": phone, "Billing Address": street, "Billing Street2": "",
            "Billing City": city, "Billing State": st, "Billing Code": zipc,
            "Shipping Address": street, "Shipping Street2": "Dock 2" if i == 1 else "",
            "Shipping City": city, "Shipping State": st, "Shipping Code": zipc,
            "Payment Terms Label": terms, "Status": "Active", "Notes": "",
        })
    return rows


def _vendors():
    base = [
        ("Mr. Joseph Cordon", "joe@cordon.example", "555-0201", "4 Dock Rd", "Houston", "TX", "77001", "Due on Receipt"),
        ("Pioneer Wholesale", "ar@pioneer.example", "555-0202", "800 Industrial Way", "Dallas", "TX", "75201", "Net 30"),
        ("Summit Packaging", "sales@summit.example", "555-0203", "55 Summit Ave", "Reno", "NV", "89501", "Net 30"),
    ]
    return [
        {"Contact ID": f"49000000{i:05d}", "Display Name": n, "EmailID": e, "Phone": p,
         "Billing Address": a, "Billing City": c, "Billing State": s, "Billing Code": z,
         "Payment Terms Label": t, "Status": "Active"}
        for i, (n, e, p, a, c, s, z, t) in enumerate(base, 1)
    ]


# ---------------------------------------------------------------- document helpers
# A line is (item key, qty, price) or (item key, qty, price, item_total) when Zoho's
# Item Total differs from qty x price (a discount).
def _sales_order(zid, number, date, status, customer, lines, *, shipping=D(0), adjustment=D(0), notes=""):
    total = sum((_line_total(l[1], l[2], l[3] if len(l) > 3 else None) for l in lines), D(0)) + shipping + adjustment
    rows = []
    for l in lines:
        name, sku, desc, *_ = _item(l[0])
        rows.append({
            "SalesOrder ID": zid, "Order Date": date, "SalesOrder Number": number, "Status": status,
            "Customer Name": customer, "Item Name": name, "SKU": sku, "Item Desc": desc,
            "QuantityOrdered": str(l[1]), "Item Price": _m(l[2]),
            "Item Total": _m(_line_total(l[1], l[2], l[3] if len(l) > 3 else None)),
            "Total": _m(total), "Shipping Charge": _m(shipping) if shipping else "0.000",
            "Adjustment": _m(adjustment) if adjustment else "0.0", "Notes": notes,
        })
    return rows


def _invoice(zid, number, date, status, customer, so, lines, *, shipping=D(0), adjustment=D(0),
             tax=D(0), terms="Net 30", notes=""):
    """tax is the invoice's tax, written on the first line as Item Tax Amount."""
    total = sum((_line_total(l[1], l[2], l[3] if len(l) > 3 else None) for l in lines), D(0)) + shipping + adjustment + tax
    rows = []
    for i, l in enumerate(lines):
        name, sku, desc, *_ = _item(l[0])
        rows.append({
            "Invoice ID": zid, "Invoice Date": date, "Invoice Number": number,
            "Invoice Status": status, "Customer Name": customer, "Sales Order Number": so,
            "Item Name": name, "SKU": sku, "Item Desc": desc, "Quantity": str(l[1]),
            "Item Price": _m(l[2]),
            "Item Total": _m(_line_total(l[1], l[2], l[3] if len(l) > 3 else None)),
            "Total": _m(total), "Shipping Charge": _m(shipping) if shipping else "0.000",
            "Shipping Charge Account": "Shipping Charge" if shipping else "",
            "Adjustment": _m(adjustment) if adjustment else "0.0",
            "Adjustment Account": "Other Charges" if adjustment else "",
            "Item Tax Amount": _m(tax) if (tax and i == 0) else "0.00", "Notes": notes,
            "Payment Terms Label": terms,
        })
    return rows


def _purchase_order(zid, number, date, vendor, lines):
    rows = []
    for l in lines:
        name, sku, *_ = _item(l[0])
        rows.append({
            "Purchase Order ID": zid, "Purchase Order Date": date, "Purchase Order Number": number,
            "Vendor Name": vendor, "Item Name": name, "SKU": sku, "QuantityOrdered": str(l[1]),
            "Item Price": _m(l[2]), "Item Total": _m(D(l[1]) * D(l[2])),
        })
    return rows


def _bill(zid, number, date, status, vendor, lines, *, po="", cf_so="", terms="Net 30"):
    """Lines are (item key, qty, rate) for stock lines, or (None, qty, rate, account, description)."""
    parts = []
    for l in lines:
        parts.append(D(l[1]) * D(l[2]))
    total = sum(parts, D(0))
    rows = []
    for l, amount in zip(lines, parts):
        if l[0] is None:
            name = sku = ""
            account, desc = l[3], l[4]
        else:
            name, sku, *_ = _item(l[0])
            account, desc = "Inventory Asset", ""
        rows.append({
            "Bill ID": zid, "Bill Date": date, "Bill Number": number, "Vendor Name": vendor,
            "Bill Status": status, "Item Name": name, "SKU": sku, "Account": account,
            "Description": desc, "Quantity": str(l[1]), "Rate": _m(l[2]), "Item Total": _m(amount),
            "Total": _m(total), "Adjustment": "0.00", "Adjustment Account": "",
            "Payment Terms Label": terms, "Purchase Order Number": po, "CF.Sales Order": cf_so,
        })
    return rows


def _cust_payment(zid, number, mode, date, amount, customer, deposit_to, applications, *,
                  unused="0.00", bank_charges="0.00", ref=""):
    """applications: [(invoice number, applied amount)]. One row per application."""
    return [{
        "CustomerPayment ID": zid, "Payment Number": number, "Mode": mode, "Date": date,
        "Amount": _m(amount), "Unused Amount": unused, "Bank Charges": bank_charges,
        "Reference Number": ref, "Customer Name": customer, "Deposit To": deposit_to,
        "Invoice Number": inv, "Amount Applied to Invoice": _m(applied),
        "Invoice Payment Applied Date": date,
    } for inv, applied in applications]


def _vendor_payment(zid, number, mode, date, vendor, paid_through, bill, amount, *, ref=""):
    return [{
        "VendorPayment ID": zid, "Payment Number": number, "Mode": mode, "Date": date,
        "Amount": _m(amount), "Reference Number": ref, "Vendor Name": vendor,
        "Paid Through": paid_through, "Bill Number": bill, "Bill Amount": _m(amount),
        "Bill Payment Applied Date": date,
    }]


# ---------------------------------------------------------------- the sample
def sample_rows() -> dict[str, list[dict[str, str]]]:
    rows: dict[str, list[dict[str, str]]] = {short: [] for short in FILES}
    rows["accounts"] = _accounts()
    rows["items"] = _items()
    rows["customers"] = _customers()
    rows["vendors"] = _vendors()
    acme, birch, cedar = CUSTOMERS
    cordon, pioneer, summit = VENDORS

    # --- Sales orders: SO-0001 sells bolts before any are received (item goes negative);
    # SO-0004 is shipped on two invoices; SO-0006 has a shipping charge; SO-0007 is drop-shipped
    # (bill BILL-004 carries CF.Sales Order); SO-0008 is void.
    so = rows["sales_orders"]
    so += _sales_order("50000000001", "SO-0001", "2024-03-01", "Closed", acme, [("D", 20, D("8.00"))])
    so += _sales_order("50000000002", "SO-0002", "2024-03-15", "Closed", birch,
                       [("A", 30, D("25.00"), D("712.50")), ("B", 10, D("30.00"))])
    so += _sales_order("50000000003", "SO-0003", "2024-09-10", "Closed", cedar, [("C", 20, D("50.00"))])
    so += _sales_order("50000000004", "SO-0004", "2024-11-20", "Closed", acme, [("A", 40, D("25.00"))])
    so += _sales_order("50000000005", "SO-0005", "2025-02-14", "Closed", birch, [("B", 20, D("30.00"))])
    so += _sales_order("50000000006", "SO-0006", "2025-05-18", "Closed", cedar,
                       [("A", 10, D("25.00"))], shipping=D("45.00"))
    so += _sales_order("50000000007", "SO-0007", "2025-06-01", "Closed", acme,
                       [("B", 10, D("30.00"))], notes="Drop ship from Summit")
    so += _sales_order("50000000008", "SO-0008", "2026-01-12", "Void", birch, [("A", 5, D("25.00"))])

    # --- Invoices. INV-2 has a discount (Item Total 712.50 vs 30 x 25.00 = 750.00).
    # INV-4 and INV-5 ship SO-0004 in two parts. INV-6 has an adjustment, INV-7 a shipping
    # charge, INV-8 an item tax amount. INV-9 has no order and a service line; it stays open.
    inv = rows["invoices"]
    inv += _invoice("51000000001", "INV-000001", "2024-03-02", "Paid", acme, "SO-0001", [("D", 20, D("8.00"))])
    inv += _invoice("51000000002", "INV-000002", "2024-03-16", "Paid", birch, "SO-0002",
                    [("A", 30, D("25.00"), D("712.50")), ("B", 10, D("30.00"))], terms="Net 15")
    inv += _invoice("51000000003", "INV-000003", "2024-09-11", "Paid", cedar, "SO-0003",
                    [("C", 20, D("50.00"))], terms="Due on Receipt")
    inv += _invoice("51000000004", "INV-000004", "2024-11-21", "Paid", acme, "SO-0004", [("A", 25, D("25.00"))])
    inv += _invoice("51000000005", "INV-000005", "2024-12-05", "Paid", acme, "SO-0004", [("A", 15, D("25.00"))])
    inv += _invoice("51000000006", "INV-000006", "2025-02-15", "Paid", birch, "SO-0005",
                    [("B", 20, D("30.00"))], adjustment=D("-0.50"), terms="Net 15")
    inv += _invoice("51000000007", "INV-000007", "2025-05-19", "Paid", cedar, "SO-0006",
                    [("A", 10, D("25.00"))], shipping=D("45.00"), terms="Due on Receipt")
    inv += _invoice("51000000008", "INV-000008", "2025-06-02", "Paid", acme, "SO-0007",
                    [("B", 10, D("30.00"))], tax=D("21.00"), notes="Drop ship")
    inv += _invoice("51000000009", "INV-000009", "2026-02-10", "Sent", cedar, "",
                    [("C", 10, D("50.00")), ("W", 2, D("5.00"))], terms="Due on Receipt")

    # --- Purchase orders and bills. BILL-002 is the first receipt of bolts.
    po = rows["purchase_orders"]
    po += _purchase_order("52000000001", "PO-0001", "2024-02-05", pioneer, [("A", 100, D("12.00")), ("B", 50, D("15.00"))])
    po += _purchase_order("52000000002", "PO-0002", "2024-07-20", summit, [("D", 100, D("3.00")), ("C", 30, D("28.00"))])
    po += _purchase_order("52000000003", "PO-0003", "2025-03-01", pioneer, [("A", 60, D("12.00"))])

    b = rows["bills"]
    b += _bill("53000000001", "BILL-001", "2024-02-12", "Paid", pioneer,
               [("A", 100, D("12.00")), ("B", 50, D("15.00"))], po="PO-0001")
    b += _bill("53000000002", "BILL-002", "2024-08-01", "Paid", summit,
               [("D", 100, D("3.00")), ("C", 30, D("28.00"))], po="PO-0002")
    b += _bill("53000000003", "BILL-003", "2025-03-08", "Open", pioneer, [("A", 60, D("12.00"))], po="PO-0003")
    b += _bill("53000000004", "BILL-004", "2025-06-02", "Open", summit, [("B", 10, D("15.00"))], cf_so="SO-0007")
    b += _bill("53000000005", "BILL-005", "2025-05-20", "Open", cordon,
               [(None, 1, D("45.00"), "Shipping Expenses", "Shipping for Order SO-0006")],
               terms="Due on Receipt")

    # --- Customer payments. Cash "1" and "3" go to Undeposited and are deposited together by
    # transfer 2. "2" (Check) and "4" (Cash) stay undeposited. 1001, CP-1002, 57, CP-1003 go
    # straight to the bank. "57" overpays (Unused Amount). CP-1003 has bank charges.
    cp = rows["customer_payments"]
    cp += _cust_payment("54000000001", "1", "Cash", "2024-03-10", D("160.00"), acme, UNDEPOSITED, [("INV-000001", D("160.00"))])
    cp += _cust_payment("54000000002", "2", "Check", "2024-04-02", D("1012.50"), birch, UNDEPOSITED,
                        [("INV-000002", D("1012.50"))], ref="CHK 4411")
    cp += _cust_payment("54000000003", "1001", "Bank Transfer", "2024-10-05", D("1000.00"), cedar, BANK,
                        [("INV-000003", D("1000.00"))], ref="ACH 9912")
    cp += _cust_payment("54000000004", "CP-1002", "Check", "2024-12-01", D("625.00"), acme, BANK,
                        [("INV-000004", D("625.00"))], ref="CHK 7781")
    cp += _cust_payment("54000000005", "3", "Cash", "2024-12-20", D("375.00"), acme, UNDEPOSITED, [("INV-000005", D("375.00"))])
    cp += _cust_payment("54000000006", "57", "Bank Transfer", "2025-02-28", D("600.00"), birch, BANK,
                        [("INV-000006", D("539.50"))], unused="60.50", ref="ACH 1180")
    cp += _cust_payment("54000000007", "4", "Cash", "2025-05-25", D("295.00"), cedar, UNDEPOSITED, [("INV-000007", D("295.00"))])
    cp += _cust_payment("54000000008", "CP-1003", "Bank Transfer", "2025-06-20", D("321.00"), acme, BANK,
                        [("INV-000008", D("321.00"))], bank_charges="3.00", ref="ACH 5520")

    # --- Vendor payments (BILL-001 by check, BILL-002 by bank transfer).
    vp = rows["vendor_payments"]
    vp += _vendor_payment("55000000001", "1", "Check", "2024-03-12", pioneer, BANK, "BILL-001", D("1950.00"), ref="5001")
    vp += _vendor_payment("55000000002", "2", "Bank Transfer", "2024-09-01", summit, BANK, "BILL-002", D("1140.00"))

    # --- Credit note CN-000001 (2 red widgets damaged) applied in full to INV-000006,
    # so INV-000006 owes 599.50 - 60.00 = 539.50, which payment "57" applies.
    rows["credit_notes"] = [{
        "CreditNotes ID": "56000000001", "Credit Note Date": "2025-02-20",
        "Credit Note Number": "CN-000001", "Customer Name": birch, "Item Name": "Red Widget",
        "Account": "Sales", "Item Total": "60.00", "Total": "60.00", "Reason": "Damaged in transit",
    }]
    rows["credit_applications"] = [{
        "Credit Note Number": "CN-000001", "Invoice Number": "INV-000006", "Amount": "60.00",
        "Date": "2025-02-25",
    }]

    # --- Vendor credit VC-0001 (10 blue widgets back to Pioneer) applied to BILL-003.
    rows["vendor_credits"] = [{
        "VendorCredit ID": "57000000001", "Vendor Credit Date": "2025-03-15",
        "Vendor Credit Number": "VC-0001", "Vendor Name": pioneer, "Item Name": "Blue Widget",
        "SKU": "WID-A", "Quantity": "10", "Account": "Inventory Asset", "Item Total": "120.00",
        "Total": "120.00", "Bill Number": "BILL-003", "Amount Applied": "120.00",
    }]

    rows["expenses"] = [
        {"Expense Reference ID": "58000000001", "Expense Date": "2024-05-05",
         "Expense Description": "Printer paper", "Expense Account": "Office Supplies",
         "Paid Through": CARD, "Expense Amount": "85.40", "Total": "85.40",
         "Reference#": "RCPT-77", "Vendor": summit},
        {"Expense Reference ID": "58000000002", "Expense Date": "2025-07-01",
         "Expense Description": "Headway interest", "Expense Account": "Interest Expense",
         "Paid Through": LOC_HEADWAY, "Expense Amount": "120.00", "Total": "120.00",
         "Reference#": "", "Vendor": ""},
    ]
    rows["journals"] = [
        {"Journal ID": "59000000001", "Journal Date": "2025-12-31", "Journal Number": "1",
         "Notes": "Reclass bank fees", "Account": "Bank Fees and Charges", "Debit": "15.00",
         "Credit": "0.00", "Description": "Fees booked as supplies"},
        {"Journal ID": "59000000001", "Journal Date": "2025-12-31", "Journal Number": "1",
         "Notes": "Reclass bank fees", "Account": "Office Supplies", "Debit": "0.00",
         "Credit": "15.00", "Description": "Fees booked as supplies"},
    ]
    rows["deposits"] = [
        {"Transaction Date": "2024-01-15", "Transaction Type": "Capital Introduced",
         "From Account": "Owner's Equity", "To Account": BANK, "Reference": "",
         "Description": "Owner investment", "Total": _m(D("5000.00"))},
        {"Transaction Date": "2025-08-01", "Transaction Type": "Expense Refund",
         "From Account": "Office Supplies", "To Account": BANK, "Reference": "REF-3",
         "Description": "Supplier refund", "Total": "20.00"},
    ]
    rows["transfers"] = [
        {"Transaction Date": "2024-06-05", "Transaction Type": "Card Payment",
         "From Account": BANK, "To Account": CARD, "Reference": "", "Description": "Card payment",
         "Total": "85.40"},
        {"Transaction Date": "2024-12-22", "Transaction Type": "Funds Transfer",
         "From Account": UNDEPOSITED, "To Account": BANK, "Reference": "DEP-1",
         "Description": "Deposit of cash", "Total": _m(EXPECTED["undeposited_transfer"])},
        {"Transaction Date": "2025-09-10", "Transaction Type": "Funds Transfer",
         "From Account": LOC_CHASE, "To Account": BANK, "Reference": "",
         "Description": "Draw on line", "Total": _m(D("2000.00"))},
    ]
    rows["adjustments"] = [
        {"InventoryAdjustment ID": "60000000001", "Date": "2025-10-01",
         "Adjustment Type": "Quantity", "Reason": "Damaged", "Description": "Water damage",
         "Item Name": "Blue Widget", "SKU": "WID-A", "Account": "Cost of Goods Sold",
         "Quantity Adjusted": "-2", "Value Adjusted": ""},
        {"InventoryAdjustment ID": "60000000002", "Date": "2025-10-02",
         "Adjustment Type": "Value", "Reason": "Revaluation", "Description": "Cost correction",
         "Item Name": "Gadget", "SKU": "GAD-C", "Account": "Cost of Goods Sold",
         "Quantity Adjusted": "", "Value Adjusted": "-50.00"},
    ]
    return rows


# ---------------------------------------------------------------- writing
def _csv_bytes(columns: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return buf.getvalue().encode("utf-8-sig")


def _split_by_document(short: str, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    id_col = ID_COLUMNS[short]
    seen: list[str] = []
    for r in rows:
        if r[id_col] not in seen:
            seen.append(r[id_col])
    first = set(seen[: ZIP_SPLIT_AFTER[short]])
    return [r for r in rows if r[id_col] in first], [r for r in rows if r[id_col] not in first]


def build_sample_export(
    tmp_path,
    *,
    drop_column: tuple[str, str] | None = None,
    extra_rows: dict[str, list[dict]] | None = None,
) -> Path:
    """Write the sample export folder under tmp_path and return it.

    drop_column=(short name, column) removes that column from the file.
    extra_rows={short name: [row dicts]} appends rows (keys are Zoho column names;
    for zipped documents they go in the last inner file).
    """
    folder = Path(tmp_path) / "zoho_export"
    folder.mkdir(parents=True, exist_ok=True)
    rows = sample_rows()
    for short, extra in (extra_rows or {}).items():
        rows[short] = rows[short] + [dict(r) for r in extra]
    for short, filename in FILES.items():
        columns = [c for c in (ID_COLUMNS[short], *REQUIRED[short]) if c]
        if drop_column and drop_column[0] == short:
            columns.remove(drop_column[1])
        if filename.endswith(".zip"):
            stem = filename[:-4]
            first, rest = _split_by_document(short, rows[short])
            inner = {f"{stem}00.csv": first, f"{stem}01.csv": rest}
            with zipfile.ZipFile(folder / filename, "w") as z:
                for name in sorted(inner, reverse=True):  # reader must sort by name
                    z.writestr(name, _csv_bytes(columns, inner[name]))
        else:
            (folder / filename).write_bytes(_csv_bytes(columns, rows[short]))
    return folder


def zip_export(folder: Path) -> Path:
    """Zip the export folder into one file, as Zoho's download is."""
    archive = shutil.make_archive(str(Path(folder).parent / "zoho_export_download"), "zip", folder)
    return Path(archive)

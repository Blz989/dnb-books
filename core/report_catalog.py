"""The reports list shown beside every report, grouped the way Zoho groups them."""
from django.urls import reverse

GROUPS = [
    ("Business overview", [
        ("profit-and-loss", "Profit and loss", "Income, cost of goods sold, and expenses for a date range."),
        ("balance-sheet", "Balance sheet", "Assets, liabilities, and equity on a date."),
        ("order-profitability", "Order profitability", "Profit per order after stock and drop-ship cost."),
        ("cash-forecast", "Cash forecast", "Cash on hand plus what's coming in, less what's going out."),
    ]),
    ("Receivables", [
        ("ar-aging", "A/R aging", "What customers owe, by how late it is."),
        ("open-invoices", "Open invoices", "Every invoice with a balance."),
        ("customer-balances", "Customer balances", "One line per customer."),
        ("open-sales-orders", "Open sales orders", "Orders not fully invoiced."),
        ("undeposited-funds", "Undeposited funds", "Payments waiting to go to the bank."),
    ]),
    ("Payables", [
        ("ap-aging", "A/P aging", "What you owe vendors, by how late it is."),
        ("bills-due", "Bills due", "Bills coming due and overdue."),
        ("vendor-balances", "Vendor balances", "One line per vendor."),
        ("open-purchase-orders", "Open purchase orders", "Orders not fully billed."),
        ("drop-ship-awaiting-bill", "Drop shipments awaiting bill", "Drop-ship lines with no vendor bill yet."),
        ("1099-summary", "1099 summary", "Payments to 1099 vendors for a year."),
    ]),
    ("Inventory", [
        ("inventory-valuation", "Inventory valuation", "Stock on hand and its value against the books."),
        ("stock-movements", "Stock movements", "Every receipt, sale, and adjustment."),
        ("low-stock", "Low stock", "Items at or below their reorder point."),
        ("negative-stock", "Negative stock", "Items below zero."),
    ]),
    ("Accountant", [
        ("trial-balance", "Trial balance", "Every account's debit or credit balance."),
        ("general-ledger", "General ledger", "Every line posted to one account."),
    ]),
]

FEATURED = ["profit-and-loss", "balance-sheet", "cash-forecast", "ar-aging", "ap-aging", "order-profitability"]


def catalog(current: str | None = None) -> list[dict]:
    return [
        {"label": label, "reports": [
            {"slug": slug, "name": name, "description": desc, "url": reverse(f"reports:{slug}"), "on": slug == current}
            for slug, name, desc in reports
        ]}
        for label, reports in GROUPS
    ]


def featured() -> list[dict]:
    flat = {r["slug"]: r for g in catalog() for r in g["reports"]}
    return [flat[s] for s in FEATURED]

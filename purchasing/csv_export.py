from ledger.csv_export import amt, csv_response


def ap_aging_csv(a):
    def line(name, r):
        return [name, amt(r.current), amt(r.d1_30), amt(r.d31_60), amt(r.d61_90), amt(r.over_90),
                amt(r.unapplied_credits), amt(r.total)]

    rows = [line(r.vendor.name, r) for r in a.rows] + [line("Total", a.totals)]
    rows.append(["A/P account", "", "", "", "", "", "", amt(a.ledger_balance)])
    rows.append(["Difference", "", "", "", "", "", "", amt(a.difference)])
    return csv_response(
        f"ap-aging-{a.as_of.isoformat()}.csv",
        ["Vendor", "Current", "1-30", "31-60", "61-90", "Over 90", "Unapplied credits", "Total"], rows)


def bills_due_csv(rows):
    body = [[r.bill.number, r.bill.vendor.name, r.bill.vendor_invoice_number, r.bill.date.isoformat(),
             r.bill.due_date.isoformat(), amt(r.balance), max(r.days_overdue, 0)] for r in rows]
    return csv_response(
        "bills-due.csv", ["Bill", "Vendor", "Vendor invoice", "Date", "Due", "Balance", "Days overdue"], body)


def vendor_balances_csv(rows):
    return csv_response("vendor-balances.csv", ["Vendor", "Balance"], [[r.vendor.name, amt(r.balance)] for r in rows])


def open_purchase_orders_csv(rows):
    body = [[r.order.number, r.order.vendor.name, r.order.date.isoformat(), r.line.item.sku, r.ordered, r.billed, r.left]
            for r in rows]
    return csv_response("open-purchase-orders.csv", ["Order", "Vendor", "Date", "SKU", "Ordered", "Billed", "Left"], body)


def summary_1099_csv(summary):
    """Full tax IDs, so the view only offers this to admins."""
    body = [[r.vendor.name, r.vendor.tax_id, r.vendor.address, amt(r.total), "yes" if r.file else "no"]
            for r in summary.rows]
    return csv_response(
        f"1099-summary-{summary.year}.csv", ["vendor", "tax_id", "address", "total_paid", "file"], body)

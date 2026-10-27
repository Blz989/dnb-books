from ledger.csv_export import amt, csv_response


def ar_aging_csv(a):
    def line(name, r):
        return [name, amt(r.current), amt(r.d1_30), amt(r.d31_60), amt(r.d61_90), amt(r.over_90),
                amt(r.unapplied_credits), amt(r.total)]

    rows = [line(r.customer.name, r) for r in a.rows] + [line("Total", a.totals)]
    rows.append(["A/R account", "", "", "", "", "", "", amt(a.ledger_balance)])
    rows.append(["Difference", "", "", "", "", "", "", amt(a.difference)])
    return csv_response(
        f"ar-aging-{a.as_of.isoformat()}.csv",
        ["Customer", "Current", "1-30", "31-60", "61-90", "Over 90", "Unapplied credits", "Total"], rows)


def open_invoices_csv(rows):
    body = [[r.invoice.number, r.invoice.customer.name, r.invoice.date.isoformat(), r.invoice.due_date.isoformat(),
             amt(r.balance), max(r.days_overdue, 0)] for r in rows]
    return csv_response("open-invoices.csv", ["Invoice", "Customer", "Date", "Due", "Balance", "Days overdue"], body)


def customer_balances_csv(rows):
    return csv_response("customer-balances.csv", ["Customer", "Balance"], [[r.customer.name, amt(r.balance)] for r in rows])


def open_sales_orders_csv(rows):
    body = [[r.order.number, r.order.customer.name, r.order.date.isoformat(), r.line.item.sku, r.line.get_fulfillment_display(),
             r.ordered, r.left] for r in rows]
    return csv_response("open-sales-orders.csv", ["Order", "Customer", "Date", "SKU", "Ship from", "Ordered", "Left"], body)


def undeposited_funds_csv(u):
    body = [[p.number, p.customer.name, p.date.isoformat(), p.get_method_display(), p.reference, amt(p.amount)] for p in u.payments]
    body.append(["Total", "", "", "", "", amt(u.total)])
    body.append(["Undeposited Funds account", "", "", "", "", amt(u.ledger_balance)])
    body.append(["Difference", "", "", "", "", amt(u.difference)])
    return csv_response("undeposited-funds.csv", ["Payment", "Customer", "Date", "Method", "Reference", "Amount"], body)


def drop_ship_csv(lines):
    body = [[l.invoice.number, l.invoice.date.isoformat(), l.invoice.customer.name, l.item.sku, l.quantity,
             l.drop_ship_vendor] for l in lines]
    return csv_response("drop-ship-awaiting-bill.csv", ["Invoice", "Date", "Customer", "SKU", "Quantity", "Vendor"], body)

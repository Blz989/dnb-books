from ledger.csv_export import amt, csv_response


def valuation_csv(v):
    rows = [[r.item.sku, r.item.name, r.quantity, amt(r.value)] for r in v.rows]
    rows.append(["", "Total", "", amt(v.total)])
    return csv_response(f"inventory-valuation-{v.as_of.isoformat()}.csv", ["SKU", "Name", "Quantity", "Value"], rows)


def movements_csv(rows, start, end):
    body = [
        [r.movement.date.isoformat(), r.movement.item.sku, r.movement.get_kind_display(), r.movement.quantity,
         f"{r.movement.unit_cost:.4f}", amt(r.effective_value), r.running_quantity,
         r.movement.journal_entry.display_number if r.movement.journal_entry else "",
         "Voided" if r.movement.voided else "", r.movement.reason]
        for r in rows
    ]
    return csv_response(
        f"stock-movements-{start.isoformat()}_{end.isoformat()}.csv",
        ["Date", "SKU", "Kind", "Quantity", "Unit cost", "Value", "Running quantity", "Entry", "Status", "Reason"],
        body)


def negative_stock_csv(rows):
    return csv_response(
        "negative-stock.csv", ["SKU", "Name", "Quantity", "Estimated value"],
        [[r.item.sku, r.item.name, r.quantity, amt(r.value)] for r in rows])

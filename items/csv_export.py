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


def low_stock_csv(rows):
    return csv_response("low-stock.csv", ["SKU", "Name", "On hand", "On order", "Reorder point", "Short by"], [
        [r.item.sku, r.item.name, r.on_hand, r.on_order, r.reorder_point, r.short_by] for r in rows])


def fifo_lots_csv(r):
    body = []
    for row in r.rows:
        lot = row.lot
        note = row.tag
        if row.first and lot.opening:
            note = "; ".join(x for x in (f"Opening, {lot.on_hand_at_start} on hand at {r.start.isoformat()}",
                                         f"{lot.used_before_start} used before {r.start.isoformat()}" if lot.used_before_start else "",
                                         note) if x)
        body.append([
            lot.date.isoformat(), lot.label, lot.party, lot.quantity, r.item.unit, lot.age_days, f"{lot.unit_cost:.4f}",
            amt(lot.total), row.out_date.isoformat() if row.out_date else "", row.out_label, row.out_party,
            row.out_quantity if row.out_quantity is not None else "", note])
    for s in r.shorts:
        body.append(["", "", "", "", r.item.unit, "", "", "", s.date.isoformat(), s.label, s.party, s.quantity,
                     f"Sold with no matching receipt, estimated {amt(s.estimate)}. {s.resolved}"])
    body.append([r.end.isoformat(), "On hand", "", r.on_hand, r.item.unit, "", "", amt(r.value), "", "", "", "",
                 f"Lots remaining {r.lots_quantity}, {amt(r.lots_value)}"])
    return csv_response(
        f"fifo-lots-{r.item.sku}-{r.start.isoformat()}_{r.end.isoformat()}.csv",
        ["In date", "In transaction", "Received from", "In quantity", "Unit", "Age", "Cost per unit", "In total",
         "Out date", "Out transaction", "Dispersed to", "Out quantity", "Note"], body)

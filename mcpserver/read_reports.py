"""The reports behind the Reports screens that run_report did not cover at first. Each calls the same function
the screen calls, so the figures match."""
from decimal import Decimal

from banking.models import BankAccount
from banking.register import RegisterFilters, build_rows, filtered_lines
from items.models import CostingMethod
from items.reports import negative_stock
from purchasing import reports as purchasing_reports
from sales import reports as sales_reports

from . import lookup, read_more
from .read import LIMIT, ZERO, _limited, _today
from .runtime import Refused, fmt

EXTRA = {
    "general_ledger": "General ledger for one account and date range (needs account)",
    "bank_register": "Bank register for one bank account and date range (needs account)",
    "stock_movements": "Stock movements in a date range, optionally one SKU",
    "fifo_lots": "FIFO lots for one item (needs sku) over a date range",
    "negative_stock": "Items whose stock is below zero",
    "customer_balances": "What each customer owes, net of unapplied payments and credits, as of a date",
    "vendor_balances": "What is owed to each vendor, net of unapplied payments and credits, as of a date",
    "open_sales_orders": "Open sales order lines with quantity left to invoice",
    "open_purchase_orders": "Open purchase order lines with quantity left to bill",
    "undeposited_funds": "Payments received but not yet deposited, against the ledger balance",
    "drop_ship_awaiting_bill": "Drop-ship invoice lines not yet billed by the vendor",
    "unpaid_invoices": "Every customer's unpaid invoices, aged, as of a date",
    "1099_summary": "Payments to 1099 vendors for a year (the year of the end date)",
}


def _aging_row(r) -> dict:
    return {"current": fmt(r.current), "1_30": fmt(r.d1_30), "31_60": fmt(r.d31_60), "61_90": fmt(r.d61_90),
            "over_90": fmt(r.over_90), "unapplied_credits": fmt(r.unapplied_credits), "total": fmt(r.total)}


def _bank_register(account: str, start_d, end_d) -> dict:
    ledger = lookup.account(account)
    bank = BankAccount.objects.filter(ledger_account=ledger).first()
    if bank is None:
        raise Refused(f"{ledger.name} is not a bank account.")
    lines = list(filtered_lines(bank, RegisterFilters(start=start_d, end=end_d)))
    rows = build_rows(bank, lines[:LIMIT])
    out = {"account": str(ledger), "from": start_d.isoformat(), "to": end_d.isoformat()}
    out.update(_limited([{"date": r.date.isoformat(), "reference": r.reference, "type": r.type_label,
                          "counterparty": r.counterparty, "status": r.status,
                          "deposit": fmt(r.deposit) if r.deposit is not None else "",
                          "withdrawal": fmt(r.withdrawal) if r.withdrawal is not None else "",
                          "balance": fmt(r.balance)} for r in rows], "rows", len(lines)))
    return out


def _fifo(sku: str, start_d, end_d) -> dict:
    from items.lots import fifo_lots

    if not sku.strip():
        raise Refused("Choose an item (sku) for the FIFO lots report.")
    item = lookup.item(sku)
    if item.costing_method != CostingMethod.FIFO:
        raise Refused(f"{item.sku} is not costed by FIFO.")
    r = fifo_lots(item, start_d, end_d)
    out = {"sku": item.sku, "from": start_d.isoformat(), "to": end_d.isoformat(), "received": r.received,
           "sold": r.sold, "on_hand": r.on_hand, "value": fmt(r.value), "lots_quantity": r.lots_quantity,
           "lots_value": fmt(r.lots_value), "difference": fmt(r.difference)}
    out.update(_limited([{"date": l.date.isoformat(), "source": f"{l.label} {l.party}".strip(),
                          "quantity": l.quantity, "unit_cost": fmt(l.unit_cost), "remaining": l.remaining_quantity,
                          "remaining_value": fmt(l.remaining_value), "age_days": l.age_days}
                         for l in r.lots], "lots", len(r.lots)))
    return out


def extra_report(user, report: str, *, as_of: str, start: str, end: str, account: str, sku: str) -> dict:
    today = _today()
    as_of_d = lookup.day(as_of, "as-of date", today)
    start_d = lookup.day(start, "start date", today.replace(month=1, day=1))
    end_d = lookup.day(end, "end date", today)
    if start_d > end_d:
        raise Refused("The start date is after the end date.")

    if report == "general_ledger":
        if not account.strip():
            raise Refused("Choose an account for the general ledger.")
        return read_more.get_account_ledger(user, account, start, end)
    if report == "bank_register":
        if not account.strip():
            raise Refused("Choose a bank account for the register.")
        return _bank_register(account, start_d, end_d)
    if report == "stock_movements":
        return read_more.list_stock_movements(user, sku, start, end)
    if report == "fifo_lots":
        return _fifo(sku, start_d, end_d)
    if report == "negative_stock":
        return _limited([{"sku": r.item.sku, "name": r.item.name, "quantity": r.quantity, "value": fmt(r.value)}
                         for r in negative_stock()], "items", len(negative_stock()))
    if report == "customer_balances":
        rows = sales_reports.customer_balances(as_of_d)
        return {"as_of": as_of_d.isoformat(), "total": fmt(sum((r.balance for r in rows), ZERO)),
                **_limited([{"customer": r.customer.name, "balance": fmt(r.balance)} for r in rows],
                           "customers", len(rows))}
    if report == "vendor_balances":
        rows = purchasing_reports.vendor_balances(as_of_d)
        return {"as_of": as_of_d.isoformat(), "total": fmt(sum((r.balance for r in rows), ZERO)),
                **_limited([{"vendor": r.vendor.name, "balance": fmt(r.balance)} for r in rows],
                           "vendors", len(rows))}
    if report == "open_sales_orders":
        rows = sales_reports.open_sales_orders()
        return _limited([{"order": r.order.number, "customer": r.order.customer.name, "date": r.order.date.isoformat(),
                          "sku": r.line.item.sku, "ordered": r.ordered, "left": r.left} for r in rows],
                        "lines", len(rows))
    if report == "open_purchase_orders":
        rows = purchasing_reports.open_purchase_orders()
        return _limited([{"order": r.order.number, "vendor": r.order.vendor.name, "date": r.order.date.isoformat(),
                          "sku": r.line.item.sku, "ordered": r.ordered, "billed": r.billed, "left": r.left}
                         for r in rows], "lines", len(rows))
    if report == "undeposited_funds":
        u = sales_reports.undeposited_funds()
        return {"total": fmt(u.total), "ledger_balance": fmt(u.ledger_balance), "difference": fmt(u.difference),
                **_limited([{"payment": p.number, "customer": p.customer.name, "date": p.date.isoformat(),
                             "method": p.get_method_display(), "amount": fmt(p.amount)} for p in u.payments],
                           "payments", len(u.payments))}
    if report == "drop_ship_awaiting_bill":
        rows = sales_reports.drop_ship_awaiting_bill()
        return _limited([{"invoice": l.invoice.number, "customer": l.invoice.customer.name,
                          "date": l.invoice.date.isoformat(), "sku": l.item.sku, "quantity": l.quantity,
                          "vendor": l.drop_ship_vendor} for l in rows], "lines", len(rows))
    if report == "unpaid_invoices":
        u = sales_reports.unpaid_by_customer(as_of_d)
        return {"as_of": as_of_d.isoformat(), "totals": _aging_row(u.totals), "ledger_balance": fmt(u.ledger_balance),
                "difference": fmt(u.difference),
                **_limited([{"customer": g.customer.name, "credits": fmt(g.credits), "total": fmt(g.totals.total),
                             "invoices": [{"number": a.invoice.number, "due": a.invoice.due_date.isoformat(),
                                           "balance": fmt(a.balance)} for a in g.invoices]}
                            for g in u.groups], "customers", len(u.groups))}
    # 1099_summary
    s = purchasing_reports.summary_1099(end_d.year)
    return {"year": s.year, "threshold": fmt(s.threshold) if s.threshold is not None else "",
            **_limited([{"vendor": r.vendor.name, "total": fmt(r.total), "needs_1099": r.file} for r in s.rows],
                       "vendors", len(s.rows))}

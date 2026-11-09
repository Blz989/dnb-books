"""Read tools. Each takes the signed-in user first and returns plain data; amounts are strings like "125.00"."""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone

from attachments.files import human_size
from attachments.service import active as active_attachments
from contacts.models import Customer, Vendor
from items.models import Item, ItemType
from items.reports import inventory_valuation, item_totals, low_stock, on_order_quantities
from ledger.comparison import balance_sheet_comparison, profit_and_loss_comparison, trial_balance_comparison
from ledger.models import EntryStatus, JournalEntry
from ledger import sources
from purchasing import reports as purchasing_reports
from purchasing.balances import bill_balance, bill_total, bills_with_balances, vendor_balance
from purchasing.models import Bill, BillPaymentApplication, DocStatus as BillStatus, OrderStatus, PurchaseOrder
from purchasing.posting.orders import billed_quantity
from sales import reports as sales_reports
from sales.balances import customer_balance, invoice_balance, invoice_total, invoices_with_balances
from sales.models import DocStatus, Invoice, Payment

from . import lookup
from .runtime import Refused, fmt

LIMIT = 50
ZERO = Decimal("0.00")


def _today():
    return timezone.localdate()


def _limited(rows: list, key: str, total: int) -> dict:
    out = {key: rows[:LIMIT]}
    if total > LIMIT:
        out["more"] = f"Showing {LIMIT} of {total}. Narrow the search to see the rest."
    return out


# ---- home ----

def home_summary(user) -> dict:
    from banking import status as banking_status
    from banking.forecast import cash_on_hand

    today = _today()
    overdue = sales_reports.open_invoices(today, overdue_only=True)
    bills = purchasing_reports.bills_due_summary(today)
    bank, undeposited = cash_on_hand(today)
    return {
        "as_of": today.isoformat(),
        "open_receivables": fmt(sales_reports.ar_aging(today).totals.total),
        "overdue_invoices": {"count": len(overdue), "total": fmt(sum((r.balance for r in overdue), ZERO))},
        "bills_due_next_7_days": {"count": bills.upcoming_count, "total": fmt(bills.upcoming_total)},
        "overdue_bills": {"count": bills.overdue_count, "total": fmt(bills.overdue_total)},
        "cash_in_bank": fmt(bank),
        "undeposited_funds": fmt(undeposited),
        "stock_value": fmt(sum((value for _, value in item_totals().values()), ZERO)),
        "items_to_reorder": len(low_stock()),
        "bank_inbox_waiting": banking_status.inbox_count(),
    }


# ---- customers ----

def search_customers(user, query: str = "", include_inactive: bool = False) -> dict:
    qs = Customer.objects.order_by("name")
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if query.strip():
        q = query.strip()
        qs = qs.filter(Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q))
    total = qs.count()
    rows = [{"name": c.name, "contact": c.contact_name, "email": c.email, "phone": c.phone, "terms": c.get_terms_display(),
             "balance": fmt(customer_balance(c)), "active": c.is_active} for c in qs[:LIMIT]]
    return _limited(rows, "customers", total)


def get_customer(user, name: str) -> dict:
    c = lookup.customer(name)
    today = _today()
    open_rows = sales_reports.open_invoices(today, customer=c)
    payments = Payment.objects.filter(customer=c).order_by("-date", "-id")[:10]
    return {
        "name": c.name, "contact": c.contact_name, "email": c.email, "phone": c.phone,
        "billing_address": c.billing_address, "ship_to_address": c.ship_to_address, "terms": c.get_terms_display(),
        "resale_certificate": c.resale_certificate, "notes": c.notes, "active": c.is_active,
        "balance": fmt(customer_balance(c)),
        "open_invoices": [{"number": r.invoice.number, "date": r.invoice.date.isoformat(),
                           "due": r.invoice.due_date.isoformat(), "balance": fmt(r.balance),
                           "days_overdue": max(r.days_overdue, 0)} for r in open_rows[:LIMIT]],
        "recent_payments": [{"number": p.number, "date": p.date.isoformat(), "method": p.get_method_display(),
                             "amount": fmt(p.amount), "void": p.status == DocStatus.VOID} for p in payments],
    }


# ---- vendors ----

def search_vendors(user, query: str = "", include_inactive: bool = False) -> dict:
    qs = Vendor.objects.order_by("name")
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if query.strip():
        q = query.strip()
        qs = qs.filter(Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q))
    total = qs.count()
    rows = [{"name": v.name, "contact": v.contact_name, "email": v.email, "phone": v.phone, "terms": v.get_terms_display(),
             "balance": fmt(vendor_balance(v)), "active": v.is_active} for v in qs[:LIMIT]]
    return _limited(rows, "vendors", total)


def get_vendor(user, name: str) -> dict:
    from purchasing.models import VendorPayment

    v = lookup.vendor(name)
    open_bills = [b for b in bills_with_balances().filter(vendor=v, status=BillStatus.OPEN).order_by("due_date")
                  if bill_balance(b) > 0]
    payments = VendorPayment.objects.filter(vendor=v).order_by("-date", "-id")[:10]
    return {
        "name": v.name, "contact": v.contact_name, "email": v.email, "phone": v.phone, "address": v.address,
        "terms": v.get_terms_display(), "gets_1099": v.is_1099,
        "tax_id": v.masked_tax_id if v.tax_id else "",
        "default_expense_account": str(v.default_expense_account) if v.default_expense_account else "",
        "notes": v.notes, "active": v.is_active, "balance": fmt(vendor_balance(v)),
        "open_bills": [{"number": b.number, "vendor_invoice": b.vendor_invoice_number, "date": b.date.isoformat(),
                        "due": b.due_date.isoformat(), "balance": fmt(bill_balance(b))} for b in open_bills[:LIMIT]],
        "recent_payments": [{"number": p.number, "date": p.date.isoformat(), "method": p.get_method_display(),
                             "amount": fmt(p.amount), "check_number": p.check_number,
                             "void": p.status == BillStatus.VOID} for p in payments],
    }


# ---- items ----

def _item_row(item, totals, on_order):
    quantity, value = totals.get(item.pk, (0, ZERO))
    return {"sku": item.sku, "name": item.name, "type": item.get_item_type_display(),
            "on_hand": quantity if item.item_type == ItemType.STOCK else None,
            "on_order": on_order.get(item.pk, 0), "sale_price": fmt(item.sale_price),
            "purchase_cost": fmt(item.purchase_cost), "active": item.is_active}


def search_items(user, query: str = "", include_inactive: bool = False) -> dict:
    qs = Item.objects.order_by("sku")
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if query.strip():
        q = query.strip()
        qs = qs.filter(Q(sku__icontains=q) | Q(name__icontains=q) | Q(vendor_part_number__icontains=q))
    total = qs.count()
    page = list(qs[:LIMIT])
    totals, on_order = item_totals([i.pk for i in page]), on_order_quantities()
    return _limited([_item_row(i, totals, on_order) for i in page], "items", total)


def get_item(user, sku: str) -> dict:
    item = lookup.item(sku)
    totals, on_order = item_totals([item.pk]), on_order_quantities()
    row = _item_row(item, totals, on_order)
    quantity, value = totals.get(item.pk, (0, ZERO))
    row.update({
        "description": item.description, "unit": item.unit, "packaging": item.packaging,
        "category": str(item.category) if item.category else "", "vendor_part_number": item.vendor_part_number,
        "reorder_point": item.reorder_point, "stock_value": fmt(value) if item.item_type == ItemType.STOCK else None,
        "income_account": str(item.income_account),
    })
    return row


# ---- invoices ----

def list_invoices(user, customer: str = "", status: str = "open", start: str = "", end: str = "") -> dict:
    today = _today()
    qs = invoices_with_balances().order_by("-date", "-id")
    if customer.strip():
        qs = qs.filter(customer=lookup.customer(customer))
    if status not in ("open", "overdue", "all", "void"):
        raise Refused("Choose a status: open, overdue, all, or void.")
    if status in ("open", "overdue"):
        qs = qs.filter(status=DocStatus.OPEN)
    if status == "overdue":
        qs = qs.filter(due_date__lt=today)
    if status == "void":
        qs = qs.filter(status=DocStatus.VOID)
    if lookup.day(start, "start date"):
        qs = qs.filter(date__gte=lookup.day(start))
    if lookup.day(end, "end date"):
        qs = qs.filter(date__lte=lookup.day(end))
    rows = []
    for inv in qs.iterator(chunk_size=200):
        balance = invoice_balance(inv)
        if status in ("open", "overdue") and balance <= 0:
            continue
        rows.append({"number": inv.number, "customer": inv.customer.name, "date": inv.date.isoformat(),
                     "due": inv.due_date.isoformat(), "total": fmt(invoice_total(inv)), "balance": fmt(balance),
                     "status": "void" if inv.status == DocStatus.VOID else ("paid" if balance <= 0 else "open")})
        if len(rows) > LIMIT:
            break
    out = {"invoices": rows[:LIMIT]}
    if len(rows) > LIMIT:
        out["more"] = f"Showing the first {LIMIT}. Narrow by customer or dates to see the rest."
    return out


def get_invoice(user, number: str) -> dict:
    inv = lookup.document(Invoice, number, "invoice")
    apps = [{"payment": a.payment.number, "date": a.applied_on.isoformat(), "amount": fmt(a.amount)}
            for a in inv.payment_applications.filter(removed=False).select_related("payment")]
    credits = [{"credit_note": a.credit_note.number, "date": a.applied_on.isoformat(), "amount": fmt(a.amount)}
               for a in inv.credit_applications.filter(removed=False).select_related("credit_note")]
    return {
        "number": inv.number, "customer": inv.customer.name, "date": inv.date.isoformat(),
        "due": inv.due_date.isoformat(), "terms": inv.get_terms_display(),
        "sales_order": inv.sales_order.number if inv.sales_order else "", "ship_to": inv.ship_to, "notes": inv.notes,
        "status": "void" if inv.status == DocStatus.VOID else "open",
        "lines": [{"sku": l.item.sku, "description": l.description or l.item.name, "quantity": l.quantity,
                   "price": fmt(l.price), "amount": fmt(l.amount), "ships": l.get_fulfillment_display()}
                  for l in inv.lines.select_related("item")],
        "total": fmt(invoice_total(inv)), "payments": apps, "credits": credits, "balance": fmt(invoice_balance(inv)),
    }


# ---- bills ----

def list_bills(user, vendor: str = "", status: str = "open", due_through: str = "") -> dict:
    today = _today()
    qs = bills_with_balances().order_by("due_date", "date", "id")
    if vendor.strip():
        qs = qs.filter(vendor=lookup.vendor(vendor))
    if status not in ("open", "overdue", "all", "void"):
        raise Refused("Choose a status: open, overdue, all, or void.")
    if status in ("open", "overdue"):
        qs = qs.filter(status=BillStatus.OPEN)
    if status == "overdue":
        qs = qs.filter(due_date__lt=today)
    if status == "void":
        qs = qs.filter(status=BillStatus.VOID)
    if status == "all":
        qs = qs.order_by("-date", "-id")
    through = lookup.day(due_through, "due-through date")
    if through:
        qs = qs.filter(due_date__lte=through)
    rows = []
    for b in qs.iterator(chunk_size=200):
        balance = bill_balance(b)
        if status in ("open", "overdue") and balance <= 0:
            continue
        rows.append({"number": b.number, "vendor": b.vendor.name, "vendor_invoice": b.vendor_invoice_number,
                     "date": b.date.isoformat(), "due": b.due_date.isoformat(), "total": fmt(bill_total(b)),
                     "balance": fmt(balance),
                     "status": "void" if b.status == BillStatus.VOID else ("paid" if balance <= 0 else "open")})
        if len(rows) > LIMIT:
            break
    out = {"bills": rows[:LIMIT]}
    if len(rows) > LIMIT:
        out["more"] = f"Showing the first {LIMIT}. Narrow by vendor or due date to see the rest."
    return out


def get_bill(user, number: str) -> dict:
    b = lookup.document(Bill, number, "bill")
    pays = [{"payment": a.payment.number, "date": a.applied_on.isoformat(), "amount": fmt(a.amount)}
            for a in BillPaymentApplication.objects.filter(bill=b, removed=False).select_related("payment")]
    return {
        "number": b.number, "vendor": b.vendor.name, "vendor_invoice": b.vendor_invoice_number,
        "date": b.date.isoformat(), "due": b.due_date.isoformat(), "terms": b.get_terms_display(),
        "purchase_order": b.purchase_order.number if b.purchase_order else "", "notes": b.notes,
        "status": "void" if b.status == BillStatus.VOID else "open",
        "item_lines": [{"sku": l.item.sku, "description": l.description or l.item.name, "quantity": l.quantity,
                        "price": fmt(l.price), "amount": fmt(l.amount), "kind": l.get_kind_display()}
                       for l in b.item_lines.select_related("item")],
        "expense_lines": [{"account": str(l.account), "description": l.description, "amount": fmt(l.amount)}
                          for l in b.expense_lines.select_related("account")],
        "total": fmt(bill_total(b)), "payments": pays, "balance": fmt(bill_balance(b)),
        "attachments": [{"name": a.original_name, "kind": a.kind, "size": human_size(a.size_bytes),
                         "uploaded_at": timezone.localtime(a.uploaded_at).isoformat(timespec="minutes"),
                         "uploaded_by": a.uploaded_by.get_username()} for a in active_attachments(b)],
    }


# ---- purchase orders ----

def list_purchase_orders(user, vendor: str = "", status: str = "open") -> dict:
    qs = PurchaseOrder.objects.select_related("vendor").order_by("-date", "-id")
    if vendor.strip():
        qs = qs.filter(vendor=lookup.vendor(vendor))
    if status not in ("open", "all", "closed"):
        raise Refused("Choose a status: open, closed, or all.")
    if status == "open":
        qs = qs.exclude(status=OrderStatus.CLOSED)
    elif status == "closed":
        qs = qs.filter(status=OrderStatus.CLOSED)
    total = qs.count()
    rows = [{"number": po.number, "vendor": po.vendor.name, "date": po.date.isoformat(),
             "expected": po.expected_date.isoformat() if po.expected_date else "", "status": po.get_status_display(),
             "total": fmt(sum((l.quantity * l.price for l in po.lines.all()), ZERO))}
            for po in qs.prefetch_related("lines")[:LIMIT]]
    return _limited(rows, "purchase_orders", total)


def get_purchase_order(user, number: str) -> dict:
    po = lookup.document(PurchaseOrder, number, "purchase order")
    lines = []
    for l in po.lines.select_related("item").prefetch_related("allocations__so_line__order__customer"):
        billed = billed_quantity(l)
        row = {"sku": l.item.sku, "description": l.description or l.item.name, "quantity": l.quantity,
               "price": fmt(l.price), "amount": fmt(l.quantity * l.price), "billed": billed,
               "remaining": max(l.quantity - billed, 0)}
        allocations = [{"sales_order": a.so_line.order.number, "customer": a.so_line.order.customer.name,
                        "quantity": a.quantity} for a in l.allocations.all()]
        if allocations:
            row["allocations"] = allocations
        lines.append(row)
    return {
        "number": po.number, "vendor": po.vendor.name, "date": po.date.isoformat(),
        "expected": po.expected_date.isoformat() if po.expected_date else "", "status": po.get_status_display(),
        "drop_ship_for": ", ".join(o.number for o in po.sales_orders()), "notes": po.notes, "lines": lines,
        "total": fmt(sum((Decimal(x["amount"]) for x in lines), ZERO)),
        "bills": list(po.bills.values_list("number", flat=True)),
    }


# ---- journal ----

def search_journal_entries(user, query: str = "", start: str = "", end: str = "", status: str = "") -> dict:
    entries = JournalEntry.objects.annotate(amount=Sum("lines__debit")).order_by("-date", "-id")
    q = query.strip()
    if q:
        number = q.upper().removeprefix("JE-")
        match = Q(memo__icontains=q) | Q(reference__icontains=q) | Q(source_id__icontains=q)
        if number.isdigit():
            match |= Q(pk=int(number))
        entries = entries.filter(match)
    if status:
        wanted = status.strip().upper()
        if wanted not in EntryStatus.values:
            raise Refused("Choose a status: draft, posted, or void.")
        entries = entries.filter(status=wanted)
    if lookup.day(start, "start date"):
        entries = entries.filter(date__gte=lookup.day(start))
    if lookup.day(end, "end date"):
        entries = entries.filter(date__lte=lookup.day(end))
    total = entries.count()
    rows = []
    for e in entries.prefetch_related("lines__account")[:LIMIT]:
        src = sources.describe(e)
        rows.append({
            "number": e.display_number, "date": e.date.isoformat(), "status": e.get_status_display(),
            "memo": e.memo, "reference": e.reference, "amount": fmt(e.amount or ZERO),
            "source": f"{src['label']} {src['number']}".strip() if src else "Manual entry",
            "lines": [{"account": str(l.account), "debit": fmt(l.debit), "credit": fmt(l.credit),
                       "description": l.description} for l in e.lines.all()],
        })
    return _limited(rows, "entries", total)


# ---- reports ----

REPORTS = {
    "trial_balance": "Trial balance as of a date (compare up to 3 years)",
    "profit_and_loss": "Profit and loss for a date range, accrual or cash basis (compare up to 3 years)",
    "balance_sheet": "Balance sheet as of a date (compare up to 3 years)",
    "ar_aging": "A/R aging by customer as of a date",
    "ap_aging": "A/P aging by vendor as of a date",
    "open_invoices": "Open invoices as of a date, optionally overdue only",
    "bills_due": "Open bills due on or before a date",
    "low_stock": "Stock items at or below their reorder point",
    "inventory_valuation": "Inventory quantity and value by item as of a date",
    "order_profitability": "Order profitability for orders dated in a range",
    "cash_forecast": "Cash forecast: view daily (30 days), weekly (13 weeks), or monthly (12 months)",
}


def _comparison(c) -> dict:
    rows = []
    for r in c.rows:
        if r.kind == "section":
            rows.append({"section": r.label})
            continue
        row = {"account": f"{r.number} {r.label}".strip(), "amounts": [fmt(a) for a in r.amounts]}
        if r.kind == "total":
            row["total"] = True
        if len(c.columns) > 1:
            row["change"] = fmt(r.change)
            row["change_percent"] = "" if r.percent is None else f"{r.percent}"
        rows.append(row)
    out = {"report": c.title, "columns": c.columns, "rows": rows}
    if c.note:
        out["note"] = c.note
    return out


def _aging(rows, name_attr, totals, as_of) -> dict:
    def row(r, name):
        return {"name": name, "current": fmt(r.current), "1_30": fmt(r.d1_30), "31_60": fmt(r.d31_60),
                "61_90": fmt(r.d61_90), "over_90": fmt(r.over_90), "unapplied_credits": fmt(r.unapplied_credits),
                "total": fmt(r.total)}
    return {"as_of": as_of.isoformat(),
            "rows": [row(r, getattr(getattr(r, name_attr), "name", "Unassigned")) for r in rows],
            "totals": row(totals, "Total")}


def run_report(user, report: str, as_of: str = "", start: str = "", end: str = "", basis: str = "accrual",
               compare_years: int = 0, view: str = "weekly", overdue_only: bool = False) -> dict:
    today = _today()
    report = (report or "").strip().lower().replace(" ", "_").replace("&", "and")
    if report not in REPORTS:
        raise Refused("Choose a report: " + ", ".join(REPORTS) + ".")
    if not 0 <= int(compare_years) <= 3:
        raise Refused("Compare up to 3 earlier years.")
    years = int(compare_years)
    as_of_d = lookup.day(as_of, "as-of date", today)
    start_d = lookup.day(start, "start date", today.replace(month=1, day=1))
    end_d = lookup.day(end, "end date", today)
    if start_d > end_d:
        raise Refused("The start date is after the end date.")

    if report == "trial_balance":
        return _comparison(trial_balance_comparison(as_of_d, years))
    if report == "balance_sheet":
        return _comparison(balance_sheet_comparison(as_of_d, years))
    if report == "profit_and_loss":
        if basis not in ("accrual", "cash"):
            raise Refused("Choose accrual or cash basis.")
        out = _comparison(profit_and_loss_comparison(start_d, end_d, years, basis=basis))
        out["basis"] = basis
        return out
    if report == "ar_aging":
        a = sales_reports.ar_aging(as_of_d)
        return _aging(a.rows, "customer", a.totals, as_of_d)
    if report == "ap_aging":
        a = purchasing_reports.ap_aging(as_of_d)
        return _aging(a.rows, "vendor", a.totals, as_of_d)
    if report == "open_invoices":
        rows = sales_reports.open_invoices(as_of_d, overdue_only=overdue_only)
        return {"as_of": as_of_d.isoformat(), "total": fmt(sum((r.balance for r in rows), ZERO)),
                **_limited([{"number": r.invoice.number, "customer": r.invoice.customer.name,
                             "due": r.invoice.due_date.isoformat(), "balance": fmt(r.balance),
                             "days_overdue": max(r.days_overdue, 0)} for r in rows], "invoices", len(rows))}
    if report == "bills_due":
        through = lookup.day(as_of, "due-through date", today + timedelta(days=7))
        rows = purchasing_reports.bills_due(through, today)
        return {"through": through.isoformat(), "total": fmt(sum((r.balance for r in rows), ZERO)),
                **_limited([{"number": r.bill.number, "vendor": r.bill.vendor.name, "due": r.bill.due_date.isoformat(),
                             "balance": fmt(r.balance), "days_overdue": max(r.days_overdue, 0)} for r in rows],
                           "bills", len(rows))}
    if report == "low_stock":
        rows = low_stock()
        return _limited([{"sku": r.item.sku, "name": r.item.name, "on_hand": r.on_hand, "on_order": r.on_order,
                          "reorder_point": r.reorder_point, "short_by": r.short_by} for r in rows], "items", len(rows))
    if report == "inventory_valuation":
        v = inventory_valuation(as_of_d)
        return {"as_of": as_of_d.isoformat(), "total": fmt(v.total), "ledger_balance": fmt(v.ledger_balance),
                "difference": fmt(v.difference),
                **_limited([{"sku": r.item.sku, "name": r.item.name, "quantity": r.quantity, "value": fmt(r.value)}
                            for r in v.rows], "items", len(v.rows))}
    if report == "order_profitability":
        from handling.profit import orders

        rows = orders(max(today, end_d), date_from=start_d, date_to=end_d)
        sales = sum((o.net_sales for o in rows), ZERO)
        profit = sum((o.profit for o in rows), ZERO)
        return {"from": start_d.isoformat(), "to": end_d.isoformat(), "orders": len(rows), "net_sales": fmt(sales),
                "cost": fmt(sum((o.cost for o in rows), ZERO)), "profit": fmt(profit),
                "margin_percent": f"{(profit / sales * 100):.1f}" if sales else "",
                **_limited([{"order": o.sales_order.number if o.sales_order else (o.invoice.number if o.invoice else o.key),
                             "customer": getattr(o.customer, "name", ""), "date": o.date.isoformat(),
                             "net_sales": fmt(o.net_sales), "cost": fmt(o.cost), "profit": fmt(o.profit)}
                            for o in rows], "rows", len(rows))}
    # cash_forecast
    from banking.forecast import VIEWS, build

    if view not in VIEWS:
        raise Refused("Choose a view: daily, weekly, or monthly.")
    f = build(today, view)
    return {"today": today.isoformat(), "view": view, "cash_in_bank": fmt(f.bank_balance),
            "undeposited": fmt(f.undeposited), "late_receivables_counted_today": fmt(f.late_receivables),
            "periods": [{"period": p.label, "opening": fmt(p.opening), "cash_in": fmt(p.cash_in),
                         "cash_out": fmt(p.cash_out), "closing": fmt(p.closing)} for p in f.periods],
            "after_last_period": {"cash_in": fmt(f.beyond_in), "cash_out": fmt(f.beyond_out)}}

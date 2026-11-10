"""Read tools for orders, payments, credits, bank accounts, the general ledger, stock movements, and month close."""
import calendar
from datetime import date
from decimal import Decimal

from banking.models import BankAccount
from banking.register import ledger_balance
from banking.status import inbox_count
from items.reports import stock_movements as stock_movement_rows
from ledger import periods
from ledger.reports import general_ledger
from purchasing import balances as pbal
from purchasing.models import DocStatus as BillStatus, VendorCredit, VendorPayment
from sales import balances as sbal
from sales.models import CreditNote, DocStatus, OrderStatus, Payment, SalesOrder
from sales.posting.orders import remaining

from . import lookup
from .read import LIMIT, ZERO, _limited, _today
from .runtime import Refused, fmt


def _status(doc, open_status) -> str:
    return "open" if doc.status == open_status else "void"


# ---- sales orders ----

def list_sales_orders(user, customer: str = "", status: str = "open") -> dict:
    if status not in ("open", "closed", "all"):
        raise Refused("Choose a status: open, closed, or all.")
    qs = SalesOrder.objects.select_related("customer").order_by("-date", "-id")
    if customer.strip():
        qs = qs.filter(customer=lookup.customer(customer))
    if status == "open":
        qs = qs.exclude(status=OrderStatus.CLOSED)
    elif status == "closed":
        qs = qs.filter(status=OrderStatus.CLOSED)
    total = qs.count()
    rows = [{"number": o.number, "customer": o.customer.name, "date": o.date.isoformat(),
             "status": o.get_status_display(),
             "total": fmt(sum((l.quantity * l.price for l in o.lines.all()), ZERO))}
            for o in qs.prefetch_related("lines")[:LIMIT]]
    return _limited(rows, "sales_orders", total)


def get_sales_order(user, number: str) -> dict:
    order = lookup.document(SalesOrder, number, "sales order")
    lines = [{"sku": l.item.sku, "description": l.description or l.item.name, "quantity": l.quantity,
              "price": fmt(l.price), "amount": fmt(l.quantity * l.price), "remaining": remaining(l),
              "ships": l.get_fulfillment_display()} for l in order.lines.select_related("item")]
    return {
        "number": order.number, "customer": order.customer.name, "date": order.date.isoformat(),
        "status": order.get_status_display(), "ship_to": order.ship_to, "notes": order.notes, "lines": lines,
        "total": fmt(sum((Decimal(l["amount"]) for l in lines), ZERO)),
        "invoices": list(order.invoices.values_list("number", flat=True)),
    }


# ---- customer payments ----

def _dated(qs, start: str, end: str):
    if lookup.day(start, "start date"):
        qs = qs.filter(date__gte=lookup.day(start))
    if lookup.day(end, "end date"):
        qs = qs.filter(date__lte=lookup.day(end))
    return qs


def list_customer_payments(user, customer: str = "", start: str = "", end: str = "") -> dict:
    qs = Payment.objects.select_related("customer", "deposited_to").order_by("-date", "-id")
    if customer.strip():
        qs = qs.filter(customer=lookup.customer(customer))
    qs = _dated(qs, start, end)
    total = qs.count()
    rows = [{"number": p.number, "customer": p.customer.name, "date": p.date.isoformat(),
             "method": p.get_method_display(), "amount": fmt(p.amount), "unapplied": fmt(sbal.payment_unapplied(p)),
             "status": _status(p, DocStatus.OPEN)} for p in qs[:LIMIT]]
    return _limited(rows, "payments", total)


def get_customer_payment(user, number: str) -> dict:
    p = lookup.document(Payment, number, "customer payment")
    return {
        "number": p.number, "customer": p.customer.name, "date": p.date.isoformat(), "method": p.get_method_display(),
        "reference": p.reference, "amount": fmt(p.amount), "deposited_to": str(p.deposited_to),
        "deposit": p.deposit.number if p.deposit else "", "status": _status(p, DocStatus.OPEN),
        "applied": [{"invoice": a.invoice.number, "date": a.applied_on.isoformat(), "amount": fmt(a.amount)}
                    for a in p.applications.filter(removed=False).select_related("invoice")],
        "unapplied": fmt(sbal.payment_unapplied(p)),
    }


# ---- vendor payments ----

def list_vendor_payments(user, vendor: str = "", start: str = "", end: str = "") -> dict:
    qs = VendorPayment.objects.select_related("vendor", "paid_from").order_by("-date", "-id")
    if vendor.strip():
        qs = qs.filter(vendor=lookup.vendor(vendor))
    qs = _dated(qs, start, end)
    total = qs.count()
    rows = [{"number": p.number, "vendor": p.vendor.name, "date": p.date.isoformat(),
             "method": p.get_method_display(), "amount": fmt(p.amount), "check_number": p.check_number,
             "unapplied": fmt(pbal.vendor_payment_unapplied(p)), "status": _status(p, BillStatus.OPEN)}
            for p in qs[:LIMIT]]
    return _limited(rows, "payments", total)


def get_vendor_payment(user, number: str) -> dict:
    p = lookup.document(VendorPayment, number, "vendor payment")
    return {
        "number": p.number, "vendor": p.vendor.name, "date": p.date.isoformat(), "method": p.get_method_display(),
        "paid_from": str(p.paid_from), "check_number": p.check_number, "reference": p.reference,
        "amount": fmt(p.amount), "status": _status(p, BillStatus.OPEN),
        "applied": [{"bill": a.bill.number, "date": a.applied_on.isoformat(), "amount": fmt(a.amount)}
                    for a in p.applications.filter(removed=False).select_related("bill")],
        "unapplied": fmt(pbal.vendor_payment_unapplied(p)),
    }


# ---- credits ----

def list_credits(user, kind: str = "customer", status: str = "open") -> dict:
    if kind not in ("customer", "vendor"):
        raise Refused("Choose a kind: customer or vendor.")
    if status not in ("open", "all", "void"):
        raise Refused("Choose a status: open, void, or all.")
    if kind == "customer":
        qs, party, total_of, unapplied_of = CreditNote.objects.select_related("customer"), "customer", \
            sbal.credit_total, sbal.credit_unapplied
    else:
        qs, party, total_of, unapplied_of = VendorCredit.objects.select_related("vendor"), "vendor", \
            pbal.credit_total, pbal.vendor_credit_unapplied
    qs = qs.order_by("-date", "-id")
    if status == "void":
        qs = qs.filter(status=DocStatus.VOID)
    elif status == "open":
        qs = qs.filter(status=DocStatus.OPEN)
    rows = []
    for c in qs.iterator(chunk_size=200):
        left = unapplied_of(c)
        if status == "open" and left <= 0:
            continue
        rows.append({"number": c.number, party: getattr(c, party).name, "date": c.date.isoformat(),
                     "reason": c.reason, "total": fmt(total_of(c)), "unapplied": fmt(left),
                     "status": _status(c, DocStatus.OPEN)})
    out = {"credits": rows[:LIMIT]}
    if len(rows) > LIMIT:
        out["more"] = f"Showing {LIMIT} of {len(rows)}. Narrow the search to see the rest."
    return out


# ---- bank accounts ----

def _bank(account: str) -> BankAccount:
    ledger = lookup.account(account)
    found = BankAccount.objects.filter(ledger_account=ledger).select_related("ledger_account").first()
    if found is None:
        raise Refused(f"{ledger.name} is not a bank account.")
    return found


def _bank_row(b: BankAccount) -> dict:
    return {"account": str(b.ledger_account), "kind": b.get_kind_display(), "mask": b.mask,
            "balance_in_books": fmt(ledger_balance(b)),
            "balance_at_bank": fmt(b.bank_balance) if b.bank_balance is not None else "",
            "bank_balance_on": b.bank_balance_on.isoformat() if b.bank_balance_on else "", "active": b.is_active}


def list_bank_accounts(user, include_inactive: bool = False) -> dict:
    qs = BankAccount.objects.select_related("ledger_account")
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return {"accounts": [_bank_row(b) for b in qs], "bank_inbox_waiting": inbox_count()}


def get_bank_account(user, account: str) -> dict:
    b = _bank(account)
    last = b.reconciliations.filter(status="FINISHED").order_by("-statement_date").first()
    row = _bank_row(b)
    row["last_reconciled_statement"] = last.statement_date.isoformat() if last else ""
    return row


# ---- stock movements and ledger ----

def list_stock_movements(user, sku: str = "", start: str = "", end: str = "") -> dict:
    today = _today()
    start_d = lookup.day(start, "start date", today.replace(day=1))
    end_d = lookup.day(end, "end date", today)
    if start_d > end_d:
        raise Refused("The start date is after the end date.")
    item = lookup.item(sku) if sku.strip() else None
    rows = stock_movement_rows(start_d, end_d, item)
    return _limited([{"date": r.movement.date.isoformat(), "sku": r.movement.item.sku,
                      "kind": r.movement.get_kind_display(), "quantity": r.movement.quantity,
                      "value": fmt(r.effective_value), "running_quantity": r.running_quantity,
                      "reason": r.movement.reason, "void": r.movement.voided} for r in rows],
                    "movements", len(rows))


def get_account_ledger(user, account: str, start: str = "", end: str = "") -> dict:
    today = _today()
    start_d = lookup.day(start, "start date", today.replace(month=1, day=1))
    end_d = lookup.day(end, "end date", today)
    if start_d > end_d:
        raise Refused("The start date is after the end date.")
    gl = general_ledger(lookup.account(account), start_d, end_d)
    out = {"account": str(gl.account), "from": start_d.isoformat(), "to": end_d.isoformat(),
           "opening_balance": fmt(gl.opening_balance), "closing_balance": fmt(gl.closing_balance)}
    out.update(_limited([{"date": l.date.isoformat(), "entry": l.entry_number, "reference": l.reference,
                          "description": l.description, "debit": fmt(l.debit), "credit": fmt(l.credit),
                          "balance": fmt(l.balance)} for l in gl.lines], "lines", len(gl.lines)))
    return out


# ---- month close ----

def close_checklist(user, month: str = "") -> dict:
    """month is YYYY-MM; it defaults to the previous month. Read only: closing stays in the app."""
    today = _today()
    if month.strip():
        try:
            year, mon = (int(x) for x in month.strip().split("-"))
            calendar.monthrange(year, mon)
        except (ValueError, calendar.IllegalMonthError):
            raise Refused("Enter the month as YYYY-MM, like 2026-10.") from None
    else:
        year, mon = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    problems = periods.close_problems(year, mon)
    closed = periods.is_closed(date(year, mon, 1))
    return {"month": f"{calendar.month_name[mon]} {year}", "closed": closed, "ready_to_close": not problems,
            "problems": problems,
            "note": "Closing and reopening a month are done in the app (Books → Month close)."}

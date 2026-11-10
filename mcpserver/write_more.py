"""More write operations: orders, credits, refunds, deposits, transfers, stock and value adjustments, journal entries.

Same contract as write.py: each takes (user, payload, flags) and returns (document number, summary). They run
inside the preview's transaction, so they are real posting-service calls that get rolled back.
"""
from decimal import Decimal

from django.utils import timezone

from banking.models import TransferKind
from banking.transfers import create_transfer
from items.forms import REASONS
from items.models import MovementKind
from items.stock import StockLine, record_movements, record_value_adjustment
from ledger.models import AccountType
from ledger.posting import LineSpec, post_entry
from purchasing.balances import bill_balance, credit_total as vendor_credit_total, vendor_credit_unapplied
from purchasing.models import Bill, VendorCreditKind
from purchasing.posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, apply_vendor_credit, create_allowance_credit, create_return_credit,
)
from sales.balances import credit_total, credit_unapplied, invoice_balance, payment_unapplied
from sales.models import CreditNote, Fulfillment, Invoice, Payment, RefundMethod
from sales.posting.deposits import make_deposit
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note, issue_refund

from . import lookup
from .runtime import Refused, fmt, money, require_perm

ZERO = Decimal("0.00")


def _entry_lines(entry) -> list[dict]:
    if entry is None:
        return []
    return [{"account": str(l.account), "debit": fmt(l.debit) if l.debit else "", "credit": fmt(l.credit) if l.credit else ""}
            for l in entry.lines.select_related("account").order_by("id")]


def _day(p):
    return lookup.day(p.get("date"), default=timezone.localdate())


def _bank_account(value: str):
    from banking.models import BankAccount

    ledger = lookup.account(value)
    found = BankAccount.objects.filter(ledger_account=ledger).first()
    if found is None:
        raise Refused(f"{ledger.name} is not a bank account.")
    return found


# ---- sales order ----

def _sales_order(user, p, flags):
    customer = lookup.customer(p["customer"])
    if not p.get("lines"):
        raise Refused("An order needs at least one line with a quantity.")
    specs = []
    for n, line in enumerate(p["lines"], 1):
        item = lookup.item(line["sku"])
        price = money(line["price"], f"price on line {n}") if line.get("price") not in (None, "") else item.sale_price
        vendor = (line.get("drop_ship_vendor") or "").strip()
        specs.append(OrderLineSpec(item, int(line["quantity"]), price, description=line.get("description") or "",
                                   fulfillment=Fulfillment.DROP_SHIP if vendor else Fulfillment.STOCK,
                                   drop_ship_vendor=vendor))
    order = create_order(customer=customer, date=_day(p), lines=specs, user=user, ship_to=p.get("ship_to") or None,
                         notes=p.get("notes") or "")
    lines = list(order.lines.select_related("item"))
    return order.number, {
        "sales_order": order.number, "customer": customer.name, "date": order.date.isoformat(),
        "lines": [{"sku": l.item.sku, "quantity": l.quantity, "price": fmt(l.price),
                   "amount": fmt(l.quantity * l.price), "ships": l.get_fulfillment_display()} for l in lines],
        "total": fmt(sum((l.quantity * l.price for l in lines), ZERO)),
        "posts": "Nothing posts to the ledger until the invoice.",
    }


# ---- credit notes, vendor credits, refunds ----

def _credit_note(user, p, flags):
    customer = lookup.customer(p["customer"])
    day = _day(p)
    specs = [CreditLineSpec(l.get("description") or "", money(l["amount"], f"amount on line {n}"),
                            lookup.account(l["account"]) if l.get("account") else None)
             for n, l in enumerate(p.get("lines") or [], 1)]
    note = create_credit_note(customer=customer, date=day, lines=specs, reason=p.get("reason") or "", user=user)
    applied = []
    for a in p.get("apply_to") or []:
        invoice = lookup.document(Invoice, a["invoice"], "invoice")
        amount = money(a["amount"], f"amount for {invoice.number}")
        apply_credit(note, invoice, amount, date=day, user=user)
        applied.append({"invoice": invoice.number, "amount": fmt(amount), "balance_after": fmt(invoice_balance(invoice))})
    return note.number, {
        "credit_note": note.number, "customer": customer.name, "date": day.isoformat(), "reason": note.reason,
        "lines": [{"account": str(l.account), "description": l.description, "amount": fmt(l.amount)}
                  for l in note.lines.select_related("account")],
        "total": fmt(credit_total(note)), "applied": applied, "unapplied": fmt(credit_unapplied(note)),
        "posts": _entry_lines(note.journal_entry),
    }


def _vendor_credit(user, p, flags):
    vendor = lookup.vendor(p["vendor"])
    day = _day(p)
    kind = (p.get("kind") or "").strip().lower()
    if kind not in ("return", "allowance"):
        raise Refused("Choose a kind: return (stock goes back to the vendor) or allowance (a price credit).")
    if kind == "return":
        specs = [ReturnLineSpec(lookup.item(l["sku"]), int(l["quantity"]),
                                money(l["amount"], f"amount on line {n}") if l.get("amount") not in (None, "") else None)
                 for n, l in enumerate(p.get("lines") or [], 1)]
        credit = create_return_credit(vendor=vendor, date=day, lines=specs, reason=p.get("reason") or "", user=user,
                                      allow_negative=flags.get("allow_negative", False))
    else:
        specs = [AllowanceLineSpec(lookup.account(l["account"]) if l.get("account") else None,
                                   money(l["amount"], f"amount on line {n}"), l.get("description") or "")
                 for n, l in enumerate(p.get("lines") or [], 1)]
        credit = create_allowance_credit(vendor=vendor, date=day, lines=specs, reason=p.get("reason") or "", user=user)
    applied = []
    for a in p.get("apply_to") or []:
        bill = lookup.document(Bill, a["bill"], "bill")
        amount = money(a["amount"], f"amount for {bill.number}") if a.get("amount") not in (None, "") else bill_balance(bill)
        apply_vendor_credit(credit, bill, amount, date=day, user=user)
        applied.append({"bill": bill.number, "amount": fmt(amount), "balance_after": fmt(bill_balance(bill))})
    return credit.number, {
        "vendor_credit": credit.number, "vendor": vendor.name, "kind": credit.get_kind_display(),
        "date": day.isoformat(), "reason": credit.reason, "total": fmt(vendor_credit_total(credit)),
        "applied": applied, "unapplied": fmt(vendor_credit_unapplied(credit)),
        "posts": _entry_lines(credit.journal_entry),
    }


def _refund(user, p, flags):
    number = (p.get("source") or "").strip().upper()
    source = Payment.objects.filter(number__iexact=number).first() or CreditNote.objects.filter(number__iexact=number).first()
    if source is None:
        raise Refused(f"No customer payment or credit note has the number {number}.")
    method = lookup.choice(p.get("method"), RefundMethod, "refund method")
    refund = issue_refund(source=source, date=_day(p), bank_account=lookup.account(p["bank_account"]), method=method,
                          amount=money(p.get("amount")), user=user, reference=p.get("reference") or "")
    left = payment_unapplied(source) if isinstance(source, Payment) else credit_unapplied(source)
    return refund.number, {
        "refund": refund.number, "customer": refund.customer.name, "from": source.number,
        "method": refund.get_method_display(), "paid_from": str(refund.bank_account), "amount": fmt(refund.amount),
        "left_on_source": fmt(left), "posts": _entry_lines(refund.journal_entry),
    }


# ---- deposits and transfers ----

def _deposit(user, p, flags):
    payments = [lookup.document(Payment, n, "customer payment") for n in p.get("payments") or []]
    deposit = make_deposit(payments=payments, bank_account=lookup.account(p["bank_account"]), date=_day(p), user=user)
    return deposit.number, {
        "deposit": deposit.number, "date": deposit.date.isoformat(), "bank_account": str(deposit.bank_account),
        "payments": [{"payment": x.number, "customer": x.customer.name, "amount": fmt(x.amount)} for x in payments],
        "total": fmt(sum((x.amount for x in payments), ZERO)), "posts": _entry_lines(deposit.journal_entry),
    }


def _transfer(user, p, flags):
    kind = lookup.choice(p.get("kind") or "TRANSFER", TransferKind, "transfer kind")
    interest_account = lookup.account(p["interest_account"]) if p.get("interest_account") else None
    transfer = create_transfer(
        date=_day(p), from_account=_bank_account(p["from_account"]), to_account=_bank_account(p["to_account"]),
        amount=money(p.get("amount")), kind=kind, user=user,
        principal=money(p["principal"], "principal") if p.get("principal") not in (None, "") else None,
        interest=money(p["interest"], "interest") if p.get("interest") not in (None, "") else None,
        interest_account=interest_account)
    return f"TR-{transfer.pk}", {
        "transfer": f"TR-{transfer.pk}", "kind": transfer.get_kind_display(), "date": transfer.date.isoformat(),
        "from": str(transfer.from_account), "to": str(transfer.to_account), "amount": fmt(transfer.amount),
        "posts": _entry_lines(transfer.journal_entry),
    }


# ---- stock ----

def _stock_adjustment(user, p, flags):
    specs = []
    for n, l in enumerate(p.get("lines") or [], 1):
        item = lookup.item(l["sku"])
        reason = (l.get("reason") or "").strip().title()
        if reason not in REASONS:
            raise Refused(f"Line {n}: choose a reason: {', '.join(REASONS)}.")
        if int(l["quantity"]) == 0:
            raise Refused(f"Line {n}: enter a quantity other than zero. Use a negative number to remove stock.")
        account = lookup.account(l["account"]) if l.get("account") else None
        if account is not None and account.type != AccountType.EXPENSE:
            raise Refused(f"Line {n}: choose an expense account.")
        note = (l.get("note") or "").strip()
        specs.append(StockLine(item, int(l["quantity"]), reason=f"{reason}: {note}" if note else reason, account=account))
    if not specs:
        raise Refused("Add at least one line.")
    moves = record_movements(kind=MovementKind.ADJUSTMENT, date=_day(p), lines=specs, user=user,
                             allow_negative=flags.get("allow_negative", False))
    entries = [m.journal_entry for m in moves if m.journal_entry_id]
    label = ", ".join(e.display_number for e in entries) or f"{len(moves)} adjustment(s)"
    return label, {
        "adjustments": [{"sku": m.item.sku, "quantity": m.quantity, "value": fmt(m.total_cost), "reason": m.reason}
                        for m in moves],
        "posts": [line for e in entries for line in _entry_lines(e)],
    }


def _value_adjustment(user, p, flags):
    item = lookup.item(p["sku"])
    move = record_value_adjustment(item=item, date=_day(p), amount=money(p.get("amount")),
                                   reason=(p.get("reason") or "").strip(),
                                   user=user, account=lookup.account(p["account"]) if p.get("account") else None)
    label = move.journal_entry.display_number if move.journal_entry_id else item.sku
    return label, {"sku": item.sku, "change": fmt(move.total_cost), "reason": move.reason,
                   "posts": _entry_lines(move.journal_entry)}


# ---- journal entry ----

def _journal_entry(user, p, flags):
    require_perm(user, "ledger.add_journalentry")
    specs = []
    for n, l in enumerate(p.get("lines") or [], 1):
        debit = money(l["debit"], f"debit on line {n}") if l.get("debit") not in (None, "") else ZERO
        credit = money(l["credit"], f"credit on line {n}") if l.get("credit") not in (None, "") else ZERO
        specs.append(LineSpec(lookup.account(l["account"]), debit, credit, (l.get("description") or "")[:200]))
    entry = post_entry(date=_day(p), lines=specs, user=user, memo=p.get("memo") or "",
                       reference=p.get("reference") or "")
    return entry.display_number, {
        "entry": entry.display_number, "date": entry.date.isoformat(), "memo": entry.memo,
        "reference": entry.reference, "lines": _entry_lines(entry),
        "total": fmt(sum((l.debit for l in entry.lines.all()), ZERO)),
    }


OPERATIONS = {
    "create_sales_order": _sales_order,
    "create_credit_note": _credit_note,
    "create_vendor_credit": _vendor_credit,
    "issue_refund": _refund,
    "make_deposit": _deposit,
    "create_transfer": _transfer,
    "adjust_stock": _stock_adjustment,
    "adjust_item_value": _value_adjustment,
    "create_journal_entry": _journal_entry,
}

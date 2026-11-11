"""Voids and edits through the MCP. Admin only, and confirming one needs the document number typed back.

A void calls the same service as the screen, so it is a reversing entry, never a delete, and a closed month still
blocks it. The preview says in plain language what will change.
"""
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from banking.transfers import void_transfer
from banking.models import Transfer
from contacts.models import Terms
from core.roles import is_admin
from items.models import StockMovement
from items.stock import void_movement
from ledger.models import JournalEntry
from ledger.posting import void_entry
from purchasing import balances as pbal
from purchasing.models import Bill, PurchaseOrder, VendorCredit, VendorPayment
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, edit_bill, void_bill
from purchasing.posting.credits import void_vendor_credit
from purchasing.posting.orders import POLineSpec, update_po
from purchasing.posting.payments import void_vendor_payment
from sales import balances as sbal
from sales.models import CreditNote, Deposit, Fulfillment, Invoice, Payment, Refund, SalesOrder
from sales.posting.deposits import void_deposit
from sales.posting.invoices import InvoiceLineSpec, edit_invoice, void_invoice
from sales.posting.orders import OrderLineSpec, update_order
from sales.posting.receipts import void_credit_note, void_payment, void_refund

from . import lookup, read, read_more
from .runtime import Refused, fmt, money, role_message

ZERO = Decimal("0.00")


def _entry_lines(entry) -> list[dict]:
    if entry is None:
        return []
    return [{"account": str(l.account), "debit": fmt(l.debit) if l.debit else "", "credit": fmt(l.credit) if l.credit else ""}
            for l in entry.lines.select_related("account").order_by("id")]


def require_admin(user) -> None:
    if not is_admin(user):
        raise Refused(role_message(user))


# ---- voids ----

@dataclass(frozen=True)
class Kind:
    label: str
    find: Callable            # number -> document
    number: Callable          # document -> its number as people see it
    party: Callable           # document -> customer or vendor name ("" when none)
    total: Callable           # document -> amount
    entry: Callable           # document -> its posted journal entry, or None
    applied: Callable         # document -> list of strings, what gets unapplied
    run: Callable             # (document, user, reason, date, flags) -> None


def _simple_number(doc) -> str:
    return doc.number


def _numbered(prefix: str, model, label: str):
    def find(number: str):
        raw = (number or "").strip().upper()
        m = re.fullmatch(rf"{prefix}-0*(\d+)", raw)
        found = model.objects.filter(pk=int(m.group(1))).first() if m else None
        if found is None:
            raise Refused(f"No {label} has the number {raw}.")
        return found
    return find


def _apps(qs, other: str):
    return [f"{getattr(a, other).number} {fmt(a.amount)}" for a in qs.filter(removed=False).select_related(other)]


def _deposit_total(d) -> Decimal:
    return sum((p.amount for p in d.all_payments.all()), ZERO)


KINDS = {
    "void_invoice": Kind(
        "invoice", lambda n: lookup.document(Invoice, n, "invoice"), _simple_number, lambda d: d.customer.name,
        sbal.invoice_total, lambda d: d.journal_entry,
        lambda d: _apps(d.payment_applications, "payment") + _apps(d.credit_applications, "credit_note"),
        lambda d, u, r, when, f: void_invoice(d, user=u, reason=r, date=when)),
    "void_customer_payment": Kind(
        "customer payment", lambda n: lookup.document(Payment, n, "customer payment"), _simple_number,
        lambda d: d.customer.name, lambda d: d.amount, lambda d: d.journal_entry,
        lambda d: _apps(d.applications, "invoice"),
        lambda d, u, r, when, f: void_payment(d, user=u, reason=r, date=when)),
    "void_bill": Kind(
        "bill", lambda n: lookup.document(Bill, n, "bill"), _simple_number, lambda d: d.vendor.name,
        pbal.bill_total, lambda d: d.journal_entry,
        lambda d: _apps(d.payment_applications, "payment") + _apps(d.credit_applications, "credit"),
        lambda d, u, r, when, f: void_bill(d, user=u, reason=r, date=when, allow_negative=f.get("allow_negative", False))),
    "void_vendor_payment": Kind(
        "vendor payment", lambda n: lookup.document(VendorPayment, n, "vendor payment"), _simple_number,
        lambda d: d.vendor.name, lambda d: d.amount, lambda d: d.journal_entry,
        lambda d: _apps(d.applications, "bill"),
        lambda d, u, r, when, f: void_vendor_payment(d, user=u, reason=r, date=when)),
    "void_credit_note": Kind(
        "credit note", lambda n: lookup.document(CreditNote, n, "credit note"), _simple_number,
        lambda d: d.customer.name, sbal.credit_total, lambda d: d.journal_entry,
        lambda d: _apps(d.applications, "invoice"),
        lambda d, u, r, when, f: void_credit_note(d, user=u, reason=r, date=when)),
    "void_vendor_credit": Kind(
        "vendor credit", lambda n: lookup.document(VendorCredit, n, "vendor credit"), _simple_number,
        lambda d: d.vendor.name, pbal.credit_total, lambda d: d.journal_entry,
        lambda d: _apps(d.applications, "bill"),
        lambda d, u, r, when, f: void_vendor_credit(d, user=u, reason=r, date=when,
                                                    allow_negative=f.get("allow_negative", False))),
    "void_refund": Kind(
        "refund", lambda n: lookup.document(Refund, n, "refund"), _simple_number, lambda d: d.customer.name,
        lambda d: d.amount, lambda d: d.journal_entry, lambda d: [],
        lambda d, u, r, when, f: void_refund(d, user=u, reason=r, date=when)),
    "void_deposit": Kind(
        "deposit", lambda n: lookup.document(Deposit, n, "deposit"), _simple_number, lambda d: "",
        _deposit_total, lambda d: d.journal_entry,
        lambda d: [f"{p.number} goes back to undeposited funds" for p in d.all_payments.all()],
        lambda d, u, r, when, f: void_deposit(d, user=u, reason=r, date=when)),
    "void_transfer": Kind(
        "transfer", _numbered("TR", Transfer, "transfer"), lambda d: f"TR-{d.pk}", lambda d: "",
        lambda d: d.amount, lambda d: d.journal_entry, lambda d: [],
        lambda d, u, r, when, f: void_transfer(d, user=u, reason=r, date=when)),
    "void_journal_entry": Kind(
        "journal entry", _numbered("JE", JournalEntry, "journal entry"), lambda d: d.display_number, lambda d: "",
        lambda d: sum((l.debit for l in d.lines.all()), ZERO), lambda d: d, lambda d: [],
        lambda d, u, r, when, f: void_entry(d, user=u, reason=r, date=when)),
    "void_stock_movement": Kind(
        "stock movement", _numbered("SM", StockMovement, "stock movement"), lambda d: f"SM-{d.pk}",
        lambda d: d.item.sku, lambda d: d.total_cost, lambda d: d.journal_entry, lambda d: [],
        lambda d, u, r, when, f: void_movement(d, user=u, reason=r, date=when,
                                               allow_negative=f.get("allow_negative", False))),
}


def _void(tool: str):
    kind = KINDS[tool]

    def operation(user, p, flags):
        require_admin(user)
        doc = kind.find(p.get("number"))
        reason = (p.get("reason") or "").strip()
        if not reason:
            raise Refused("Enter a reason for the void.")
        number, party, total = kind.number(doc), kind.party(doc), kind.total(doc)
        entry, applied = kind.entry(doc), kind.applied(doc)
        effect = f"This will void {kind.label} {number}" + (f" for {party}" if party else "") + f" ({fmt(total)})."
        if entry is not None:
            effect += f" It reverses journal entry {entry.display_number} with a new reversing entry."
        if applied:
            effect += f" It will unapply: {', '.join(applied)}."
        kind.run(doc, user, reason, lookup.day(p.get("date"), "void date"), flags)
        doc.refresh_from_db()
        after = kind.entry(doc)
        reversal = entry.reversals.order_by("-id").first() if entry is not None else None
        if reversal is None and after is not None and after.pk != getattr(entry, "pk", None):
            reversal = after
        return number, {"void": number, "effect": effect, "reason": reason,
                        "reversing_entry": reversal.display_number if reversal else "", "posts": _entry_lines(reversal)}
    return operation


# ---- edits ----

def _terms(value, current):
    return lookup.choice(value, Terms, "terms") if value else current


def _pick(existing: list, sku: str, used: set):
    """The first not-yet-used existing line for this SKU, so an edit keeps its link to the order it came from."""
    for line in existing:
        if line.pk not in used and line.item.sku.lower() == sku.strip().lower():
            used.add(line.pk)
            return line
    return None


def _line_fulfillment(line: dict, old) -> tuple[str, str]:
    """Fulfillment and vendor for an edited line. A vendor name sets drop ship, "" sets stock, and leaving it out
    keeps the matched existing line as it was (a new line with nothing given is stock)."""
    given = line.get("drop_ship_vendor")
    if given is None and old is not None:
        return old.fulfillment, old.drop_ship_vendor
    vendor = (given or "").strip()
    return (Fulfillment.DROP_SHIP if vendor else Fulfillment.STOCK), vendor


def _posting_effect(what: str, old, new) -> str:
    if old is not None and new is not None:
        return f"The old posting {old.display_number} is voided and a new entry is posted for the edited {what}."
    if old is not None:
        return f"The old posting {old.display_number} is voided. The edited {what} totals zero, so nothing new posts."
    if new is not None:
        return f"The {what} had no posting before. A new entry is posted for the edited {what}."
    return f"The {what} totals zero, so nothing posts to the ledger."


def _edit_invoice(user, p, flags):
    require_admin(user)
    inv = lookup.document(Invoice, p["number"], "invoice")
    before = read.get_invoice(user, inv.number)
    existing, used, specs = list(inv.lines.select_related("item")), set(), []
    if not p.get("lines"):
        raise Refused("An invoice needs at least one line with a quantity.")
    for n, l in enumerate(p["lines"], 1):
        item = lookup.item(l["sku"])
        old = _pick(existing, l["sku"], used)
        price = money(l["price"], f"price on line {n}") if l.get("price") not in (None, "") \
            else (old.price if old else item.sale_price)
        fulfillment, vendor = _line_fulfillment(l, old)
        specs.append(InvoiceLineSpec(
            item, int(l["quantity"]), price, description=l.get("description") or "",
            fulfillment=fulfillment, drop_ship_vendor=vendor, so_line=old.so_line if old else None))
    old_entry = inv.journal_entry
    edit_invoice(inv, user=user, date=lookup.day(p.get("date"), "date", inv.date), lines=specs,
                 terms=_terms(p.get("terms"), inv.terms), ship_to=p["ship_to"] if p.get("ship_to") is not None else inv.ship_to,
                 notes=p["notes"] if p.get("notes") is not None else inv.notes, reason=(p.get("reason") or "").strip(),
                 allow_negative=flags.get("allow_negative", False))
    inv.refresh_from_db()
    return inv.number, {
        "edit": inv.number, "before": before, "after": read.get_invoice(user, inv.number),
        "effect": _posting_effect("invoice", old_entry, inv.journal_entry),
        "posts": _entry_lines(inv.journal_entry)}


def _edit_bill(user, p, flags):
    require_admin(user)
    bill = lookup.document(Bill, p["number"], "bill")
    before = read.get_bill(user, bill.number)
    existing, used, item_specs = list(bill.item_lines.select_related("item")), set(), []
    for n, l in enumerate(p.get("item_lines") or [], 1):
        item = lookup.item(l["sku"])
        old = _pick(existing, l["sku"], used)
        price = money(l["price"], f"price on line {n}") if l.get("price") not in (None, "") \
            else (old.price if old else item.purchase_cost)
        item_specs.append(BillItemSpec(item, int(l["quantity"]), price, description=l.get("description") or "",
                                       po_line=old.po_line if old else None))
    expense_specs = [BillExpenseSpec(lookup.account(e["account"]), money(e["amount"], f"amount on expense line {n}"),
                                     description=e.get("description") or "")
                     for n, e in enumerate(p.get("expense_lines") or [], len(item_specs) + 1)]
    if not item_specs and not expense_specs:
        raise Refused("A bill needs at least one line.")
    old_entry = bill.journal_entry
    edit_bill(bill, user=user, date=lookup.day(p.get("date"), "date", bill.date),
              vendor_invoice_number=p["vendor_invoice_number"] if p.get("vendor_invoice_number") is not None
              else bill.vendor_invoice_number,
              item_lines=item_specs, expense_lines=expense_specs, terms=_terms(p.get("terms"), bill.terms),
              notes=p["notes"] if p.get("notes") is not None else bill.notes, reason=(p.get("reason") or "").strip(),
              allow_duplicate=flags.get("allow_duplicate", False), allow_negative=flags.get("allow_negative", False))
    bill.refresh_from_db()
    return bill.number, {
        "edit": bill.number, "before": before, "after": read.get_bill(user, bill.number),
        "effect": _posting_effect("bill", old_entry, bill.journal_entry),
        "posts": _entry_lines(bill.journal_entry)}


def _edit_sales_order(user, p, flags):
    require_admin(user)
    order = lookup.document(SalesOrder, p["number"], "sales order")
    before = read_more.get_sales_order(user, order.number)
    existing, used, specs = list(order.lines.select_related("item")), set(), []
    for n, l in enumerate(p.get("lines") or [], 1):
        item = lookup.item(l["sku"])
        old = _pick(existing, l["sku"], used)
        price = money(l["price"], f"price on line {n}") if l.get("price") not in (None, "") \
            else (old.price if old else item.sale_price)
        fulfillment, vendor = _line_fulfillment(l, old)
        specs.append(OrderLineSpec(item, int(l["quantity"]), price, description=l.get("description") or "",
                                   fulfillment=fulfillment, drop_ship_vendor=vendor, line_id=old.pk if old else None))
    update_order(order, lines=specs, user=user, date=lookup.day(p.get("date"), "date"),
                 ship_to=p.get("ship_to"), notes=p.get("notes"))
    return order.number, {"edit": order.number, "before": before, "after": read_more.get_sales_order(user, order.number),
                          "effect": "Orders don't post to the ledger, so nothing posts."}


def _edit_purchase_order(user, p, flags):
    require_admin(user)
    po = lookup.document(PurchaseOrder, p["number"], "purchase order")
    if list(po.sales_orders()):
        raise Refused(f"{po.number} is a drop-ship order. Edit it in the app.")
    before = read.get_purchase_order(user, po.number)
    existing, used, specs = list(po.lines.select_related("item")), set(), []
    for n, l in enumerate(p.get("lines") or [], 1):
        item = lookup.item(l["sku"])
        old = _pick(existing, l["sku"], used)
        price = money(l["price"], f"price on line {n}") if l.get("price") not in (None, "") \
            else (old.price if old else item.purchase_cost)
        specs.append(POLineSpec(item, int(l["quantity"]), price, description=l.get("description") or "",
                                line_id=old.pk if old else None))
    update_po(po, lines=specs, user=user, date=lookup.day(p.get("date"), "date"),
              expected_date=lookup.day(p.get("expected_date"), "expected date"), notes=p.get("notes"))
    return po.number, {"edit": po.number, "before": before, "after": read.get_purchase_order(user, po.number),
                       "effect": "Orders don't post to the ledger, so nothing posts."}


OPERATIONS = {tool: _void(tool) for tool in KINDS}
OPERATIONS.update({
    "edit_invoice": _edit_invoice,
    "edit_bill": _edit_bill,
    "edit_sales_order": _edit_sales_order,
    "edit_purchase_order": _edit_purchase_order,
})
GUARDED = frozenset(OPERATIONS)  # confirming any of these needs the document number typed back

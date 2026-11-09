"""Write tools: preview, then confirm.

A preview runs the real posting service inside a transaction and rolls it back, so it shows exactly what would
post (number, totals, accounts) and fails with exactly the message the app would show. Nothing is saved but a
PendingChange holding the input. confirm_change runs the same service again for real.
"""
import secrets
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from attachments import links
from contacts.forms import CustomerForm
from contacts.models import Customer, Terms, Vendor
from items.errors import NegativeStockWarning
from items.forms import ItemForm
from items.models import Category, Item
from items.sku import advance_past, next_sku
from purchasing.balances import bill_balance
from purchasing.forms import VendorForm
from purchasing.models import Bill, PurchaseOrder, VendorPaymentMethod
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, DuplicateBillWarning, create_bill
from purchasing.posting.orders import POLineSpec, create_po, po_remaining
from purchasing.posting.payments import pay_vendor
from sales.balances import invoice_balance, invoice_total
from sales.models import Fulfillment, Invoice, PaymentMethod, SalesOrder
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order
from sales.posting.orders import remaining
from sales.posting.receipts import oldest_first, receive_payment

from . import lookup
from .models import PendingChange
from .runtime import Refused, fmt, money, require_perm

EXPIRES = timedelta(minutes=15)
EXPIRED = "That preview expired. Ask for it again."
USED = "That change was already made."
CHANGED = "Something changed since the preview: {warning} Ask for a new preview."
LINK_TOOLS = set(links.TOOL_MODEL)  # previews that offer an upload link for the bill or receipt


class _Rollback(Exception):
    def __init__(self, summary):
        self.summary = summary


def _entry_lines(entry) -> list[dict]:
    if entry is None:
        return []
    return [{"account": str(l.account), "debit": fmt(l.debit) if l.debit else "", "credit": fmt(l.credit) if l.credit else ""}
            for l in entry.lines.select_related("account").order_by("id")]


def _terms(value: str | None):
    return lookup.choice(value, Terms, "terms") if value else None


# ---- the operations: each takes (user, payload, flags) and returns (document number, summary dict) ----

def _invoice(user, p, flags):
    customer = lookup.customer(p["customer"])
    day = lookup.day(p.get("date"), default=timezone.localdate())
    lines = p.get("lines") or []
    if not lines:
        raise Refused("An invoice needs at least one line with a quantity.")
    allow_negative = flags.get("allow_negative", False)
    if p.get("sales_order"):
        order = lookup.document(SalesOrder, p["sales_order"], "sales order")
        if order.customer_id != customer.pk:
            raise Refused(f"{order.number} belongs to a different customer.")
        quantities, open_lines = {}, list(order.lines.select_related("item"))
        for n, line in enumerate(lines, 1):
            match = [l for l in open_lines if l.item.sku.lower() == line["sku"].strip().lower()
                     and remaining(l) - quantities.get(l.pk, 0) > 0]
            if not match:
                raise Refused(f"Line {n}: {line['sku']} has nothing left to invoice on {order.number}.")
            quantities[match[0].pk] = quantities.get(match[0].pk, 0) + int(line["quantity"])
        invoice = invoice_from_order(order, date=day, quantities=quantities, user=user, allow_negative=allow_negative,
                                     ship_to=p.get("ship_to") or None, notes=p.get("notes"), terms=_terms(p.get("terms")))
    else:
        specs = []
        for n, line in enumerate(lines, 1):
            item = lookup.item(line["sku"])
            price = money(line["price"], f"price on line {n}") if line.get("price") not in (None, "") else item.sale_price
            vendor = (line.get("drop_ship_vendor") or "").strip()
            specs.append(InvoiceLineSpec(
                item, int(line["quantity"]), price, description=line.get("description") or "",
                fulfillment=Fulfillment.DROP_SHIP if vendor else Fulfillment.STOCK, drop_ship_vendor=vendor))
        invoice = create_invoice(customer=customer, date=day, lines=specs, user=user, terms=_terms(p.get("terms")),
                                 ship_to=p.get("ship_to") or None, notes=p.get("notes"), allow_negative=allow_negative)
    return invoice.number, {
        "invoice": invoice.number, "customer": customer.name, "date": invoice.date.isoformat(),
        "due": invoice.due_date.isoformat(), "terms": invoice.get_terms_display(),
        "sales_order": invoice.sales_order.number if invoice.sales_order else "",
        "lines": [{"sku": l.item.sku, "description": l.description or l.item.name, "quantity": l.quantity,
                   "price": fmt(l.price), "amount": fmt(l.amount), "ships": l.get_fulfillment_display()}
                  for l in invoice.lines.select_related("item")],
        "total": fmt(invoice_total(invoice)), "posts": _entry_lines(invoice.journal_entry),
    }


def _payment(user, p, flags):
    customer = lookup.customer(p["customer"])
    day = lookup.day(p.get("date"), default=timezone.localdate())
    method = lookup.choice(p.get("method"), PaymentMethod, "payment method")
    amount = money(p.get("amount"))
    bank = lookup.account(p["bank_account"]) if p.get("bank_account") else None
    if p.get("apply_to"):
        applications = [(lookup.document(Invoice, a["invoice"], "invoice"), money(a["amount"], f"amount for {a['invoice']}"))
                        for a in p["apply_to"]]
    else:
        applications = oldest_first(customer, amount)
    payment = receive_payment(customer=customer, date=day, method=method, amount=amount, user=user,
                              reference=p.get("reference") or "", bank_account=bank, applications=applications)
    applied = sum((a for _, a in applications), Decimal("0"))
    return payment.number, {
        "payment": payment.number, "customer": customer.name, "date": day.isoformat(),
        "method": payment.get_method_display(), "amount": fmt(amount), "deposited_to": str(payment.deposited_to),
        "applied": [{"invoice": inv.number, "amount": fmt(a), "balance_after": fmt(invoice_balance(inv))}
                    for inv, a in applications],
        "unapplied": fmt(amount - applied), "posts": _entry_lines(payment.journal_entry),
    }


def _bill(user, p, flags):
    vendor = lookup.vendor(p["vendor"])
    day = lookup.day(p.get("date"), default=timezone.localdate())
    po = lookup.document(PurchaseOrder, p["purchase_order"], "purchase order") if p.get("purchase_order") else None
    if po is not None and po.vendor_id != vendor.pk:
        raise Refused(f"{po.number} is for a different vendor.")
    item_specs, taken = [], {}
    for n, line in enumerate(p.get("item_lines") or [], 1):
        item = lookup.item(line["sku"])
        quantity = int(line["quantity"])
        po_line = None
        if po is not None:
            for l in po.lines.filter(item=item):
                if po_remaining(l) - taken.get(l.pk, 0) > 0:
                    po_line = l
                    taken[l.pk] = taken.get(l.pk, 0) + quantity
                    break
        if line.get("price") not in (None, ""):
            price = money(line["price"], f"price on line {n}")
        else:
            price = po_line.price if po_line is not None else item.purchase_cost
        item_specs.append(BillItemSpec(item, quantity, price, description=line.get("description") or "", po_line=po_line))
    expense_specs = [BillExpenseSpec(lookup.account(e["account"]), money(e["amount"], f"amount on expense line {n}"),
                                     description=e.get("description") or "")
                     for n, e in enumerate(p.get("expense_lines") or [], len(item_specs) + 1)]
    bill = create_bill(vendor=vendor, date=day, vendor_invoice_number=p.get("vendor_invoice_number") or "",
                       item_lines=item_specs, expense_lines=expense_specs, user=user, purchase_order=po,
                       terms=_terms(p.get("terms")), notes=p.get("notes") or "",
                       allow_duplicate=flags.get("allow_duplicate", False),
                       allow_negative=flags.get("allow_negative", False))
    from purchasing.balances import bill_total

    return bill.number, {
        "bill": bill.number, "vendor": vendor.name, "vendor_invoice": bill.vendor_invoice_number,
        "date": bill.date.isoformat(), "due": bill.due_date.isoformat(),
        "purchase_order": bill.purchase_order.number if bill.purchase_order else "",
        "item_lines": [{"sku": l.item.sku, "quantity": l.quantity, "price": fmt(l.price), "amount": fmt(l.amount),
                        "kind": l.get_kind_display()} for l in bill.item_lines.select_related("item")],
        "expense_lines": [{"account": str(l.account), "description": l.description, "amount": fmt(l.amount)}
                          for l in bill.expense_lines.select_related("account")],
        "total": fmt(bill_total(bill)), "posts": _entry_lines(bill.journal_entry),
    }


def _vendor_payment(user, p, flags):
    vendor = lookup.vendor(p["vendor"])
    day = lookup.day(p.get("date"), default=timezone.localdate())
    method = lookup.choice(p.get("method"), VendorPaymentMethod, "payment method")
    paid_from = lookup.account(p["paid_from"]) if p.get("paid_from") else None
    applications = []
    for b in p.get("bills") or []:
        bill = lookup.document(Bill, b["bill"], "bill")
        applications.append((bill, money(b["amount"], f"amount for {bill.number}") if b.get("amount") not in (None, "")
                             else bill_balance(bill)))
    amount = money(p["amount"]) if p.get("amount") not in (None, "") else sum((a for _, a in applications), Decimal("0"))
    payment = pay_vendor(vendor=vendor, date=day, method=method, amount=amount, paid_from=paid_from, user=user,
                         check_number=p.get("check_number") or "", reference=p.get("reference") or "",
                         applications=applications)
    applied = sum((a for _, a in applications), Decimal("0"))
    out = {
        "payment": payment.number, "vendor": vendor.name, "date": day.isoformat(), "method": payment.get_method_display(),
        "paid_from": str(paid_from), "amount": fmt(amount),
        "applied": [{"bill": bill.number, "amount": fmt(a), "balance_after": fmt(bill_balance(bill))}
                    for bill, a in applications],
        "unapplied": fmt(amount - applied), "posts": _entry_lines(payment.journal_entry),
    }
    if method == VendorPaymentMethod.CHECK:
        out["check"] = (f"Check {payment.check_number}, recorded as handwritten." if payment.check_number
                        else "No check number: it goes to Banking → Checks to print.")
    return payment.number, out


def _purchase_order(user, p, flags):
    vendor = lookup.vendor(p["vendor"])
    day = lookup.day(p.get("date"), default=timezone.localdate())
    specs = []
    for n, line in enumerate(p.get("lines") or [], 1):
        item = lookup.item(line["sku"])
        price = money(line["price"], f"price on line {n}") if line.get("price") not in (None, "") else item.purchase_cost
        specs.append(POLineSpec(item, int(line["quantity"]), price, description=line.get("description") or ""))
    po = create_po(vendor=vendor, date=day, lines=specs, user=user,
                   expected_date=lookup.day(p.get("expected_date"), "expected date"), notes=p.get("notes") or "")
    lines = list(po.lines.select_related("item"))
    return po.number, {
        "purchase_order": po.number, "vendor": vendor.name, "date": day.isoformat(),
        "expected": po.expected_date.isoformat() if po.expected_date else "",
        "lines": [{"sku": l.item.sku, "quantity": l.quantity, "price": fmt(l.price), "amount": fmt(l.quantity * l.price)}
                  for l in lines],
        "total": fmt(sum((l.quantity * l.price for l in lines), Decimal("0"))),
        "posts": "Nothing posts to the ledger until the bill.",
    }


# ---- contacts and items through the same forms the screens use ----

def _form_data(form_class, instance, **form_kwargs) -> dict:
    fields = list(form_class._meta.fields)
    data = {k: v for k, v in model_to_dict(instance, fields=fields).items() if v is not None}
    data["is_active"] = instance.is_active
    if "tax_id" in fields:  # non-admins see a masked tax ID; sending it back keeps the stored one
        data["tax_id"] = form_class(instance=instance, **form_kwargs).initial.get("tax_id", "")
    return data


def _form_errors(form) -> Refused:
    messages = []
    for field, errors in form.errors.items():
        label = form.fields[field].label if field in form.fields else ""
        for e in errors:
            messages.append(f"{label}: {e}" if label and field != "__all__" else e)
    return Refused(" ".join(messages))


def _saved_summary(obj, form, created: bool, skip=("tax_id",)) -> dict:
    shown = {k: (str(v) if v is not None else "") for k, v in form.cleaned_data.items() if k not in skip}
    if "tax_id" in form.cleaned_data and form.cleaned_data["tax_id"]:
        shown["tax_id"] = "(set, hidden)"
    return {"action": "create" if created else "update", "changed": [] if created else list(form.changed_data),
            "record": shown}


def _apply_fields(data: dict, fields: dict, accounts=(), allowed=()) -> dict:
    for key, value in (fields or {}).items():
        if key not in allowed:
            raise Refused(f"{key} can't be set here. Fields: {', '.join(allowed)}.")
        if key in accounts and value not in (None, ""):
            value = lookup.account(str(value)).pk
        if key == "category" and value not in (None, ""):
            cat = Category.objects.filter(name__iexact=str(value).strip()).first()
            if cat is None:
                raise Refused(f"No category named {value}.")
            value = cat.pk
        if key == "terms" and value:
            value = lookup.choice(str(value), Terms, "terms")
        data[key] = "" if value is None else value
    return data


CUSTOMER_FIELDS = ("name", "contact_name", "email", "phone", "billing_address", "ship_to_address", "terms",
                   "resale_certificate", "notes", "is_active")
VENDOR_FIELDS = ("name", "contact_name", "email", "phone", "address", "terms", "default_expense_account", "is_1099",
                 "tax_id", "notes", "is_active")
ITEM_FIELDS = ("sku", "name", "description", "item_type", "category", "unit", "packaging", "sale_price",
               "purchase_cost", "vendor_part_number", "costing_method", "income_account", "cogs_account",
               "inventory_account", "reorder_point", "is_active")


def _customer(user, p, flags):
    existing = Customer.objects.filter(name__iexact=p["name"].strip()).first()
    require_perm(user, "contacts.change_customer" if existing else "contacts.add_customer")
    data = _form_data(CustomerForm, existing) if existing else {"name": p["name"].strip(), "terms": Terms.NET_30}
    _apply_fields(data, p.get("fields"), allowed=CUSTOMER_FIELDS)
    form = CustomerForm(data, instance=existing)
    if not form.is_valid():
        raise _form_errors(form)
    obj = form.save()
    if existing and "is_active" in form.cleaned_data and obj.is_active != form.cleaned_data["is_active"]:
        obj.is_active = form.cleaned_data["is_active"]
        obj.save()
    return obj.name, _saved_summary(obj, form, existing is None)


def _vendor(user, p, flags):
    existing = Vendor.objects.filter(name__iexact=p["name"].strip()).first()
    require_perm(user, "contacts.change_vendor" if existing else "contacts.add_vendor")
    data = _form_data(VendorForm, existing, user=user) if existing else {"name": p["name"].strip(), "terms": Terms.NET_30}
    _apply_fields(data, p.get("fields"), accounts=("default_expense_account",), allowed=VENDOR_FIELDS)
    form = VendorForm(data, instance=existing, user=user)
    if not form.is_valid():
        raise _form_errors(form)
    obj = form.save()
    return obj.name, _saved_summary(obj, form, existing is None)


def _item(user, p, flags):
    sku = (p.get("sku") or "").strip()
    existing = Item.objects.filter(sku__iexact=sku).first() if sku else None
    require_perm(user, "items.change_item" if existing else "items.add_item")
    if existing:
        data = _form_data(ItemForm, existing)
    else:
        data = {"sku": sku or next_sku(), "unit": "each", "sale_price": "0", "purchase_cost": "0"}
    _apply_fields(data, p.get("fields"), accounts=("income_account", "cogs_account", "inventory_account"),
                  allowed=ITEM_FIELDS)
    form = ItemForm(data, instance=existing)
    if not form.is_valid():
        raise _form_errors(form)
    obj = form.save()
    if existing and "is_active" in data and obj.is_active != bool(data["is_active"]):
        obj.is_active = bool(data["is_active"])
        obj.save()
    advance_past(obj.sku)
    return obj.sku, _saved_summary(obj, form, existing is None)


OPERATIONS = {
    "create_invoice": _invoice,
    "receive_payment": _payment,
    "create_bill": _bill,
    "pay_bills": _vendor_payment,
    "create_purchase_order": _purchase_order,
    "save_customer": _customer,
    "save_vendor": _vendor,
    "save_item": _item,
}


# ---- preview and confirm ----

def _dry_run(tool, user, payload, flags):
    try:
        with transaction.atomic():
            number, summary = OPERATIONS[tool](user, payload, flags)
            raise _Rollback((number, summary))
    except _Rollback as done:
        return done.summary


def preview(user, operation: str, payload: dict) -> dict:
    tool = operation
    """Validate by running the change and rolling it back. Warnings the app would ask about are shown, not posted."""
    flags, warnings = {}, []
    while True:
        try:
            number, summary = _dry_run(tool, user, payload, flags)
            break
        except NegativeStockWarning as w:
            if flags.get("allow_negative"):
                raise
            flags["allow_negative"] = True
            warnings.extend(w.messages)
        except DuplicateBillWarning as w:
            if flags.get("allow_duplicate"):
                raise
            flags["allow_duplicate"] = True
            warnings.append(str(w).removesuffix(" Save anyway?"))
    change = PendingChange.objects.create(
        id=secrets.token_urlsafe(16), user=user, tool=tool, payload={"input": payload, "flags": flags},
        summary="", expires_at=timezone.now() + EXPIRES)
    if tool.startswith("save_"):
        note = "Nothing is saved yet."
    else:
        note = f"Nothing is saved yet. The number {number} is the next free one and may change if someone saves first."
    result = {"preview": summary, "warnings": warnings, "confirmation_id": change.id,
              "expires_in_minutes": int(EXPIRES.total_seconds() // 60),
              "next_step": "Show this to the person. Call confirm_change with the confirmation_id only after they agree.",
              "note": note}
    if tool in LINK_TOOLS:
        if links.enabled():
            link = links.make_link(user, pending_change=change)
            result.update(upload_link=links.url(link), upload_link_expires=link.expires_at.isoformat(),
                          attach_note=f"To attach the bill or receipt, open this link: {links.url(link)}")
        else:
            result["attach_note"] = links.LINK_OFF
    return result


def _clock(when) -> str:
    t = timezone.localtime(when)
    return f"{t.hour % 12 or 12}:{t:%M} {t:%p}"


def _saved(change, number) -> str:
    """The confirm message. Held files from the preview's upload links join the document here, in the same
    transaction as the posting."""
    if change.tool not in LINK_TOOLS:
        return f"Saved {number}."
    change_links = list(change.upload_links.all())
    if not change_links:
        return f"Saved {number}."
    document = links.TOOL_MODEL[change.tool].objects.get(number=number)
    count = links.attach_held(change, document)
    if count:
        return f"Saved {number} with {count} file{'s' if count != 1 else ''} attached."
    until = max(l.expires_at for l in change_links)
    return f"Saved {number}. No files attached yet; the upload link works until {_clock(until)}."


def confirm(user, confirmation_id: str) -> dict:
    with transaction.atomic():
        change = PendingChange.objects.filter(pk=(confirmation_id or "").strip()).first()
        if change is None or change.user_id != user.pk:
            raise Refused("No preview has that confirmation_id. Ask for it again.")
        if change.used_at is not None:
            raise Refused(USED)
        if timezone.now() >= change.expires_at:
            raise Refused(EXPIRED)
        # Claim it first: the conditional update makes a second confirm of the same id fail.
        if not PendingChange.objects.filter(pk=change.pk, used_at__isnull=True).update(used_at=timezone.now()):
            raise Refused(USED)
        try:
            number, summary = OPERATIONS[change.tool](user, change.payload["input"], change.payload["flags"])
        except (NegativeStockWarning, DuplicateBillWarning) as w:
            raise Refused(CHANGED.format(warning=str(w).removesuffix(" Save anyway?"))) from None
        PendingChange.objects.filter(pk=change.pk).update(document=number)
        done = _saved(change, number)
    return {"done": done, "document": number, "result": summary}


def attach_file(user, document: str) -> dict:
    """A one-time upload link for a bill or payment that already exists. It changes no figures."""
    number = (document or "").strip()
    target = None
    for model in links.TOOL_MODEL.values():
        target = model.objects.filter(number__iexact=number).first()
        if target is not None:
            break
    if target is None:
        raise Refused(f"No bill or payment numbered {number}.")
    if not links.enabled():
        raise Refused(links.LINK_OFF)
    link = links.make_link(user, target=target)
    return {"upload_link": links.url(link), "upload_link_expires": link.expires_at.isoformat(),
            "note": f"Open this link to attach files to {target.number}."}

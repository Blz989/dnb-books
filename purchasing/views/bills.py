from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contacts.models import Vendor
from core.roles import is_admin, require_admin
from items.errors import NegativeStockWarning, StockError
from ledger.errors import PostingError
from sales.forms import VoidDocForm
from sales.models import DocStatus, OrderStatus

from ..balances import bill_applied, bill_balance, bill_total
from ..errors import PurchasingError
from ..forms import BillFromPOForm, BillHeaderForm
from ..models import Bill, BillLineKind, BillPaymentApplication, PurchaseOrderLine, VendorCreditApplication
from ..posting.bills import (
    BillExpenseSpec, BillItemSpec, DuplicateBillWarning, bill_from_po, create_bill, edit_bill, void_bill,
)
from ..posting.orders import po_remaining
from ..posting.payments import remove_bill_application
from . import common
from .orders import po_or_404

PAGE_SIZE = 50
ALREADY_SAVED = "This bill was already saved."
CHANGES_SAVED = "These changes were already saved."


def bill_or_404(number: str) -> Bill:
    bill = Bill.objects.filter(number=number).select_related("vendor", "purchase_order", "journal_entry").first()
    if bill is None:
        raise Http404("No bill has that number.")
    return bill


def _today():
    return timezone.localdate()


@login_required
def bill_list(request):
    bills = Bill.objects.select_related("vendor")
    status = request.GET.get("status", "")
    if status in DocStatus.values:
        bills = bills.filter(status=status)
    vendor = request.GET.get("vendor", "")
    if common.safe_id(vendor):
        bills = bills.filter(vendor_id=common.safe_id(vendor))
    q = request.GET.get("q", "").strip()
    if q:
        bills = bills.filter(Q(number__icontains=q) | Q(vendor__name__icontains=q) | Q(vendor_invoice_number__icontains=q))
    overdue = request.GET.get("overdue") == "1"
    if overdue:
        bills = bills.filter(status=DocStatus.OPEN, due_date__lt=_today())
    page = Paginator(bills, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = []
    for bill in page:
        balance = bill_balance(bill)
        rows.append({
            "bill": bill, "total": bill_total(bill), "balance": balance,
            "overdue": bill.status == DocStatus.OPEN and balance > 0 and bill.due_date < _today(),
        })
    qs = "&".join(f"{k}={v}" for k, v in (("status", status), ("vendor", vendor), ("q", q), ("overdue", "1" if overdue else "")) if v)
    return render(request, "purchasing/bill_list.html", {
        "rows": rows, "page": page, "status": status, "vendor": vendor, "q": q, "overdue": overdue,
        "querystring": qs, "vendors": Vendor.objects.all(), "nav_active": "bills",
    })


def _item_specs(parsed, po=None):
    specs = []
    for p in parsed:
        po_line = None
        if common.safe_id(p["ref"]) and po is not None:
            po_line = PurchaseOrderLine.objects.filter(pk=common.safe_id(p["ref"]), order=po).first()
        specs.append(BillItemSpec(p["item"], p["quantity"], p["price"], description=p["description"], po_line=po_line))
    return specs


def _expense_specs(parsed):
    return [BillExpenseSpec(p["account"], p["amount"], p["description"]) for p in parsed]


def _form_context(form, rows, exp_rows, token, bill=None, **extra):
    return {
        "form": form, "rows": rows, "exp_rows": exp_rows, "token": token, "bill": bill,
        "shown": common.shown_count(rows), "exp_shown": common.expense_shown(exp_rows),
        "sku_options": common.sku_options(), "expense_accounts": common.expense_accounts(), "nav_active": "bills",
        **extra,
    }


def _confirm(request, title, *, warnings, button, flag, drop, **extra):
    return render(request, "purchasing/confirm.html", {
        "hidden": common.hidden_fields(request.POST, drop=drop), "action": request.path, "title": title,
        "warnings": warnings, "button": button, "flag": flag, "nav_active": extra.pop("nav_active", "bills"), **extra})


def _read_form(request):
    rows = common.compact(common.read_rows(request.POST))
    exp_rows = common.compact_expense(common.read_expense_rows(request.POST))
    parsed, errors = common.parse_item_rows(rows)
    filled = sum(1 for r in rows if not common.is_blank(r))
    exp_parsed, exp_errors = common.parse_expense_rows(exp_rows, start=filled)
    return rows, exp_rows, parsed, exp_parsed, errors + exp_errors


@login_required
def bill_new(request):
    if request.GET.get("po"):
        po = po_or_404(request.GET["po"])
        return redirect(reverse("purchasing:po-bill", args=[po.number]))
    if request.method != "POST":
        initial = {"date": _today().isoformat()}
        if request.GET.get("vendor", "").isdigit():
            initial["vendor"] = request.GET["vendor"]
        form = BillHeaderForm(initial=initial)
        return render(request, "purchasing/bill_form.html", _form_context(
            form, common.compact([]), common.compact_expense([]), common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("purchasing:bill-list"))
    form = BillHeaderForm(request.POST)
    rows, exp_rows, parsed, exp_parsed, errors = _read_form(request)
    if request.POST.get("back"):
        return render(request, "purchasing/bill_form.html", _form_context(form, rows, exp_rows, token))
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("purchasing:bill-list"))
                bill = create_bill(
                    vendor=d["vendor"], date=d["date"], vendor_invoice_number=d["vendor_invoice_number"],
                    item_lines=_item_specs(parsed), expense_lines=_expense_specs(exp_parsed), user=request.user,
                    terms=d["terms"] or None, notes=d["notes"],
                    allow_duplicate=request.POST.get("allow_duplicate") == "1",
                    allow_negative=request.POST.get("allow_negative") == "1")
        except DuplicateBillWarning as warning:
            return _confirm(request, "Confirm bill", warnings=[str(warning)], button="Save anyway",
                            flag="allow_duplicate", drop=("allow_duplicate",))
        except NegativeStockWarning as warning:
            return _confirm(request, "Confirm bill", warnings=warning.messages, button="Save anyway",
                            flag="allow_negative", drop=("allow_negative",))
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/bill_form.html", _form_context(
                form, rows, exp_rows, token, **common.error_context(exc)))
        messages.success(request, f"Saved {bill.number}.")
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    return render(request, "purchasing/bill_form.html", _form_context(form, rows, exp_rows, token, errors=errors))


@login_required
def bill_edit(request, number):
    bill = bill_or_404(number)
    if bill.status == DocStatus.VOID:
        messages.error(request, f"{bill.number} is void and can't be edited.")
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    if request.method != "POST":
        form = BillHeaderForm(editing=True, initial={
            "vendor": bill.vendor_id, "vendor_invoice_number": bill.vendor_invoice_number, "date": bill.date.isoformat(),
            "terms": bill.terms, "notes": bill.notes})
        rows = common.item_rows_from_lines(bill.item_lines.select_related("item"), ref=lambda l: l.po_line_id)
        exp_rows = common.expense_rows_from_lines(bill.expense_lines.all())
        return render(request, "purchasing/bill_form.html", _form_context(form, rows, exp_rows, common.new_token(), bill))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, CHANGES_SAVED)
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    form = BillHeaderForm(request.POST, editing=True)
    rows, exp_rows, parsed, exp_parsed, errors = _read_form(request)
    if request.POST.get("back"):
        return render(request, "purchasing/bill_form.html", _form_context(form, rows, exp_rows, token, bill))
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, CHANGES_SAVED)
                    return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
                edit_bill(
                    bill, user=request.user, date=d["date"], vendor_invoice_number=d["vendor_invoice_number"],
                    item_lines=_item_specs(parsed, bill.purchase_order), expense_lines=_expense_specs(exp_parsed),
                    terms=d["terms"] or None, notes=d["notes"], vendor=d["vendor"], reason=d["reason"],
                    allow_duplicate=request.POST.get("allow_duplicate") == "1",
                    allow_negative=request.POST.get("allow_negative") == "1")
        except DuplicateBillWarning as warning:
            return _confirm(request, "Confirm changes", warnings=[str(warning)], button="Save anyway",
                            flag="allow_duplicate", drop=("allow_duplicate",), bill=bill)
        except NegativeStockWarning as warning:
            return _confirm(request, "Confirm changes", warnings=warning.messages, button="Save anyway",
                            flag="allow_negative", drop=("allow_negative",), bill=bill)
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/bill_form.html", _form_context(
                form, rows, exp_rows, token, bill, **common.error_context(exc)))
        messages.success(request, f"Saved {bill.number}.")
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    return render(request, "purchasing/bill_form.html", _form_context(form, rows, exp_rows, token, bill, errors=errors))


def _po_rows(po, post=None):
    rows = []
    for line in po.lines.select_related("item"):
        left = po_remaining(line)
        if post is None:
            qty = str(left) if left > 0 else "0"
            price = common.price_text(line.price)
        else:
            qty = post.get(f"qty-{line.pk}", "").strip()
            price = post.get(f"price-{line.pk}", "").strip()
        rows.append({"line": line, "left": left, "qty": qty, "price": price})
    return rows


@login_required
def po_bill(request, number):
    """Bill part or all of a purchase order: quantities start at what's left, prices at the PO price."""
    po = po_or_404(number)
    context = {"order": po, "nav_active": "purchase-orders", "expense_accounts": common.expense_accounts()}
    if po.status == OrderStatus.CLOSED:
        messages.error(request, f"{po.number} is closed. Reopen it to bill it.")
        return redirect(reverse("purchasing:po-detail", args=[po.number]))
    if request.method != "POST":
        form = BillFromPOForm(initial={"date": _today().isoformat()})
        exp_rows = common.compact_expense([])
        return render(request, "purchasing/bill_from_po.html", {
            **context, "form": form, "rows": _po_rows(po), "exp_rows": exp_rows,
            "exp_shown": common.expense_shown(exp_rows), "token": common.new_token()})
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, ALREADY_SAVED)
        return redirect(reverse("purchasing:po-detail", args=[po.number]))
    form = BillFromPOForm(request.POST)
    rows = _po_rows(po, request.POST)
    exp_rows = common.compact_expense(common.read_expense_rows(request.POST))
    page = {**context, "form": form, "rows": rows, "exp_rows": exp_rows, "exp_shown": common.expense_shown(exp_rows),
            "token": token}
    if request.POST.get("back"):
        return render(request, "purchasing/bill_from_po.html", page)
    quantities, prices, errors = {}, {}, []
    for n, r in enumerate(rows, 1):
        try:
            quantities[r["line"].pk] = int(r["qty"] or "0")
        except ValueError:
            errors.append(f"Line {n}: enter a quantity above zero, or 0 to skip it.")
            continue
        if r["price"]:
            try:
                price = Decimal(r["price"].replace(",", ""))
                if not price.is_finite():
                    raise InvalidOperation
                prices[r["line"].pk] = price
            except InvalidOperation:
                errors.append(f"Line {n}: enter a price like 12.50.")
    filled = sum(1 for q in quantities.values() if q != 0)
    exp_parsed, exp_errors = common.parse_expense_rows(exp_rows, start=filled)
    errors += exp_errors
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, ALREADY_SAVED)
                    return redirect(reverse("purchasing:po-detail", args=[po.number]))
                bill = bill_from_po(
                    po, date=d["date"], vendor_invoice_number=d["vendor_invoice_number"], quantities=quantities,
                    prices=prices, expense_lines=_expense_specs(exp_parsed), user=request.user,
                    terms=d["terms"] or None, notes=d["notes"],
                    allow_duplicate=request.POST.get("allow_duplicate") == "1")
        except DuplicateBillWarning as warning:
            return _confirm(request, "Confirm bill", warnings=[str(warning)], button="Save anyway",
                            flag="allow_duplicate", drop=("allow_duplicate",), nav_active="purchase-orders")
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/bill_from_po.html", {**page, **common.error_context(exc)})
        messages.success(request, f"Saved {bill.number}.")
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    return render(request, "purchasing/bill_from_po.html", {**page, "errors": errors})


@login_required
def bill_detail(request, number):
    bill = bill_or_404(number)
    item_lines = list(bill.item_lines.select_related("item", "stock_movement__journal_entry", "po_line"))
    for line in item_lines:
        if line.kind == BillLineKind.DROP_SHIP:
            allocations = list(line.allocations.select_related("invoice_line__invoice__customer"))
            line.drop_ship_allocations = allocations
            line.drop_ship_unmatched = line.quantity - sum(a.quantity for a in allocations)
    expense_lines = list(bill.expense_lines.select_related("account"))
    payment_apps = list(bill.payment_applications.select_related("payment").order_by("applied_on", "id"))
    credit_apps = list(bill.credit_applications.select_related("credit").order_by("applied_on", "id"))
    entries = [("Bill", bill.journal_entry)] if bill.journal_entry_id else []
    for line in item_lines:
        m = line.stock_movement
        if m and m.journal_entry_id:
            entries.append((f"Receipt of {line.item.sku}", m.journal_entry))
    return render(request, "purchasing/bill_detail.html", {
        "bill": bill, "item_lines": item_lines, "expense_lines": expense_lines, "total": bill_total(bill),
        "applied": bill_applied(bill), "balance": bill_balance(bill), "payment_apps": payment_apps,
        "credit_apps": credit_apps, "revisions": bill.revisions.all(), "entries": entries,
        "has_drop_ship": any(l.kind == BillLineKind.DROP_SHIP for l in item_lines),
        "is_void": bill.status == DocStatus.VOID, "is_admin": is_admin(request.user), "nav_active": "bills",
    })


@login_required
def bill_revision(request, number, revision):
    bill = bill_or_404(number)
    rev = get_object_or_404(bill.revisions, revision=revision)
    vendor = Vendor.objects.filter(pk=rev.snapshot["vendor_id"]).first()
    return render(request, "purchasing/bill_revision.html", {
        "bill": bill, "rev": rev, "snap": rev.snapshot, "vendor": vendor, "nav_active": "bills"})


@login_required
def bill_void(request, number):
    require_admin(request.user)
    bill = bill_or_404(number)
    context = {"bill": bill, "nav_active": "bills"}
    if request.method != "POST":
        return render(request, "purchasing/bill_void.html", {**context, "form": VoidDocForm(initial={"date": bill.date.isoformat()})})
    form = VoidDocForm(request.POST)
    if form.is_valid():
        try:
            void_bill(bill, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"],
                      allow_negative=request.POST.get("allow_negative") == "1")
        except NegativeStockWarning as warning:
            return _confirm(request, f"Confirm void of {bill.number}", warnings=warning.messages, button="Void anyway",
                            flag="allow_negative", drop=("allow_negative",), bill=bill)
        except (PurchasingError, PostingError, StockError) as exc:
            return render(request, "purchasing/bill_void.html", {**context, "form": form, **common.error_context(exc)})
        messages.success(request, f"{bill.number} was voided.")
        return redirect(reverse("purchasing:bill-detail", args=[bill.number]))
    return render(request, "purchasing/bill_void.html", {**context, "form": form})


@login_required
@require_POST
def application_remove(request, number, kind, pk):
    bill = bill_or_404(number)
    model = {"payment": BillPaymentApplication, "credit": VendorCreditApplication}.get(kind)
    if model is None:
        raise Http404
    app = get_object_or_404(model, pk=pk, bill=bill)
    try:
        remove_bill_application(app, date=max(_today(), app.applied_on), user=request.user)
    except (PurchasingError, PostingError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Application removed.")
    return redirect(reverse("purchasing:bill-detail", args=[bill.number]))

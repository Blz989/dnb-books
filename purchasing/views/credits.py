from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from contacts.models import Vendor
from core.roles import is_admin
from items.errors import NegativeStockWarning, StockError
from items.models import Item
from ledger.errors import PostingError
from sales.models import DocStatus

from ..balances import credit_total, vendor_credit_unapplied
from ..errors import PurchasingError
from ..forms import VendorCreditHeaderForm
from ..models import VendorCredit, VendorCreditKind
from ..posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, apply_vendor_credit, create_allowance_credit, create_return_credit,
    void_vendor_credit,
)
from . import common
from .payments import apply_screen, void_screen

PAGE_SIZE = 50
ERRORS = (PurchasingError, PostingError, StockError)
RETURN_ROWS = 10
ALLOWANCE_ROWS = 8


def _today():
    return timezone.localdate()


def credit_or_404(number: str) -> VendorCredit:
    credit = VendorCredit.objects.filter(number=number).select_related("vendor").first()
    if credit is None:
        raise Http404("No vendor credit has that number.")
    return credit


@login_required
def credit_list(request):
    credits = VendorCredit.objects.select_related("vendor")
    vendor = request.GET.get("vendor", "")
    if common.safe_id(vendor):
        credits = credits.filter(vendor_id=common.safe_id(vendor))
    q = request.GET.get("q", "").strip()
    if q:
        credits = credits.filter(Q(number__icontains=q) | Q(vendor__name__icontains=q))
    page = Paginator(credits, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"credit": c, "total": credit_total(c), "unapplied": vendor_credit_unapplied(c)} for c in page]
    qs = "&".join(f"{k}={request.GET[k]}" for k in ("vendor", "q") if request.GET.get(k))
    return render(request, "purchasing/credit_list.html", {
        "rows": rows, "page": page, "vendor": vendor, "q": q, "querystring": qs, "vendors": Vendor.objects.all(),
        "nav_active": "vendor-credits",
    })


# ---- rows ----

def _return_rows(post=None):
    rows = []
    for i in range(RETURN_ROWS):
        rows.append({
            "index": i, "sku": post.get(f"ret-{i}-sku", "").strip() if post else "",
            "quantity": post.get(f"ret-{i}-quantity", "").strip() if post else "",
            "amount": post.get(f"ret-{i}-amount", "").strip() if post else "",
        })
    return rows


def _allowance_rows(post=None):
    rows = []
    for i in range(ALLOWANCE_ROWS):
        rows.append({
            "index": i, "account": post.get(f"lines-{i}-account", "").strip() if post else "",
            "amount": post.get(f"lines-{i}-amount", "").strip() if post else "",
            "description": post.get(f"lines-{i}-description", "").strip() if post else "",
        })
    return rows


def _shown(rows, keys):
    last = max([r["index"] for r in rows if any(r[k] for k in keys)] + [-1])
    return min(max(last + 2, 3), len(rows))


def _parse_return(rows):
    specs, errors, n = [], [], 0
    for r in rows:
        if not r["sku"] and not r["quantity"]:
            continue
        n += 1
        item = Item.objects.filter(sku__iexact=r["sku"]).first() if r["sku"] else None
        if not r["sku"]:
            errors.append(f"Line {n}: choose an item.")
            continue
        if item is None:
            errors.append(f"Line {n}: no item has SKU {r['sku']}.")
            continue
        try:
            quantity = int(r["quantity"])
        except ValueError:
            errors.append(f"Line {n}: enter a quantity above zero.")
            continue
        amount = None
        if r["amount"]:
            try:
                amount = Decimal(r["amount"].replace(",", ""))
                if not amount.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                errors.append(f"Line {n}: enter an amount above zero.")
                continue
        specs.append(ReturnLineSpec(item, quantity, amount))
    return specs, errors


def _parse_allowance(rows):
    from ledger.models import Account, AccountType

    specs, errors, n = [], [], 0
    for r in rows:
        if not r["account"] and not r["amount"] and not r["description"]:
            continue
        n += 1
        account = None
        if r["account"]:
            account = Account.objects.filter(
                pk=common.safe_id(r["account"]), type=AccountType.EXPENSE, is_active=True).first() \
                if common.safe_id(r["account"]) else None
            if account is None:
                errors.append(f"Line {n}: choose an account from the list.")
                continue
        try:
            amount = Decimal(r["amount"].replace(",", ""))
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"Line {n}: enter an amount above zero.")
            continue
        specs.append(AllowanceLineSpec(account, amount, r["description"]))
    return specs, errors


def _form_context(kind, form, rows, token, **extra):
    shown = _shown(rows, ("sku", "quantity", "amount") if kind == "return" else ("account", "amount", "description"))
    return {"kind": kind, "form": form, "rows": rows, "token": token, "shown": shown,
            "sku_options": common.sku_options(), "expense_accounts": common.expense_accounts(),
            "nav_active": "vendor-credits", **extra}


@login_required
def credit_new(request):
    kind = "allowance" if request.GET.get("kind") == "allowance" else "return"
    if request.method != "POST":
        initial = {"date": _today().isoformat()}
        if request.GET.get("vendor", "").isdigit():
            initial["vendor"] = request.GET["vendor"]
        rows = _return_rows() if kind == "return" else _allowance_rows()
        return render(request, "purchasing/credit_form.html", _form_context(
            kind, VendorCreditHeaderForm(initial=initial), rows, common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This credit was already saved.")
        return redirect(reverse("purchasing:credit-list"))
    form = VendorCreditHeaderForm(request.POST)
    rows = _return_rows(request.POST) if kind == "return" else _allowance_rows(request.POST)
    if request.POST.get("back"):
        return render(request, "purchasing/credit_form.html", _form_context(kind, form, rows, token))
    specs, errors = _parse_return(rows) if kind == "return" else _parse_allowance(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This credit was already saved.")
                    return redirect(reverse("purchasing:credit-list"))
                if kind == "return":
                    credit = create_return_credit(
                        vendor=d["vendor"], date=d["date"], lines=specs, reason=d["reason"], user=request.user,
                        allow_negative=request.POST.get("allow_negative") == "1")
                else:
                    credit = create_allowance_credit(
                        vendor=d["vendor"], date=d["date"], lines=specs, reason=d["reason"], user=request.user)
        except NegativeStockWarning as warning:
            return render(request, "purchasing/confirm.html", {
                "hidden": common.hidden_fields(request.POST, drop=("allow_negative",)), "action": request.get_full_path(),
                "title": "Confirm return", "warnings": warning.messages, "button": "Save anyway",
                "flag": "allow_negative", "nav_active": "vendor-credits"})
        except ERRORS as exc:
            return render(request, "purchasing/credit_form.html", _form_context(kind, form, rows, token, **common.error_context(exc)))
        messages.success(request, f"Saved {credit.number}.")
        return redirect(reverse("purchasing:credit-detail", args=[credit.number]))
    return render(request, "purchasing/credit_form.html", _form_context(kind, form, rows, token, errors=errors))


@login_required
def credit_detail(request, number):
    credit = credit_or_404(number)
    entries = [("Credit", credit.journal_entry)] if credit.journal_entry_id else []
    lines = list(credit.lines.select_related("item", "account", "stock_movement__journal_entry"))
    for line in lines:
        if line.stock_movement_id and line.stock_movement.journal_entry_id:
            entries.append((f"Cost of {line.item.sku} going back", line.stock_movement.journal_entry))
    return render(request, "purchasing/credit_detail.html", {
        "credit": credit, "lines": lines, "total": credit_total(credit), "unapplied": vendor_credit_unapplied(credit),
        "applications": credit.applications.select_related("bill").order_by("applied_on", "id"), "entries": entries,
        "is_void": credit.status == DocStatus.VOID, "is_admin": is_admin(request.user), "nav_active": "vendor-credits",
    })


@login_required
def credit_apply(request, number):
    credit = credit_or_404(number)
    unapplied = vendor_credit_unapplied(credit)
    back = reverse("purchasing:credit-detail", args=[number])
    context = {"doc": credit, "doc_kind": "credit", "unapplied": unapplied, "back": back, "nav_active": "vendor-credits"}
    if credit.status == DocStatus.VOID:
        messages.error(request, f"{credit.number} is void.")
        return redirect(back)
    return apply_screen(request, credit, credit.vendor, context, lambda bill, amount, when: apply_vendor_credit(
        credit, bill, amount, date=when, user=request.user), unapplied)


@login_required
def credit_void(request, number):
    credit = credit_or_404(number)
    return void_screen(request, credit, "vendor credit", void_vendor_credit,
                       reverse("purchasing:credit-detail", args=[number]), "vendor-credits")

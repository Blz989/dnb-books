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
from core.roles import is_admin, require_admin
from items.errors import StockError
from ledger.errors import PostingError
from sales.forms import ApplyForm, VoidDocForm
from sales.models import DocStatus

from ..balances import bill_balance, bills_with_balances, vendor_payment_unapplied
from ..errors import PurchasingError
from ..forms import PayDetailsForm, PayFilterForm, VendorPaymentForm, pay_from_accounts
from ..models import VendorPayment, VendorPaymentMethod
from ..posting.payments import PayLine, apply_vendor_payment, pay_bills, pay_vendor, void_vendor_payment
from . import common

PAGE_SIZE = 50
ERRORS = (PurchasingError, PostingError, StockError)


def _today():
    return timezone.localdate()


def payment_or_404(number: str) -> VendorPayment:
    payment = VendorPayment.objects.filter(number=number).select_related("vendor", "paid_from").first()
    if payment is None:
        raise Http404("No vendor payment has that number.")
    return payment


def open_bill_rows(*, vendor=None, through=None, post=None, prefix="pay", fill=None):
    """Open bills with a balance, soonest due first, with the amount typed (or a fill) for each."""
    bills = bills_with_balances().filter(status=DocStatus.OPEN).select_related("vendor")
    if vendor is not None:
        bills = bills.filter(vendor=vendor)
    if through is not None:
        bills = bills.filter(due_date__lte=through)
    rows = []
    for bill in bills.order_by("vendor__name", "due_date", "date", "id"):
        balance = bill_balance(bill)
        if balance <= 0:
            continue
        if post is not None:
            value = post.get(f"{prefix}-{bill.pk}", "").strip()
        else:
            value = f"{fill[bill.pk]:.2f}" if fill and bill.pk in fill else ""
        rows.append({"bill": bill, "balance": balance, "value": value})
    return rows


def read_amounts(rows):
    """[(bill, amount)] for the rows with an amount typed, and any messages about amounts that don't read."""
    chosen, errors = [], []
    for r in rows:
        if not r["value"]:
            continue
        try:
            amount = Decimal(r["value"].replace(",", ""))
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"{r['bill'].number}: enter an amount like 25.00.")
            continue
        if amount != 0:
            chosen.append((r["bill"], amount))
    return chosen, errors


def oldest_first(vendor, amount: Decimal) -> dict:
    fill, left = {}, amount
    for r in open_bill_rows(vendor=vendor):
        if left <= 0:
            break
        take = min(r["balance"], left)
        fill[r["bill"].pk] = take
        left -= take
    return fill


# ---- Pay bills ----

def _pay_context(request, filter_form, details, rows, token, **extra):
    vendors = []
    seen = set()
    for r in rows:
        if r["bill"].vendor_id not in seen:
            seen.add(r["bill"].vendor_id)
            vendors.append(r["bill"].vendor)
    source = request.POST if request.method == "POST" else {}
    return {
        "filter_form": filter_form, "details": details, "rows": rows, "token": token,
        "vendors": [{"vendor": v, "check": source.get(f"check-{v.pk}", "")} for v in vendors],
        "pay_accounts": pay_from_accounts(), "nav_active": "pay-bills", **extra,
    }


@login_required
def pay_bills_view(request):
    data = request.POST if request.method == "POST" else request.GET
    filter_form = PayFilterForm(data)
    filters_ok = filter_form.is_valid()
    vendor = filter_form.cleaned_data.get("vendor") if filters_ok else None
    through = filter_form.cleaned_data.get("through") if filters_ok else None
    if request.method != "POST":
        details = PayDetailsForm(initial={"date": _today().isoformat(), "method": "ACH"})
        rows = open_bill_rows(vendor=vendor, through=through)
        return render(request, "purchasing/pay_bills.html", _pay_context(request, filter_form, details, rows, common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "These payments were already saved.")
        return redirect(reverse("purchasing:payment-list"))
    details = PayDetailsForm(request.POST)
    rows = open_bill_rows(vendor=vendor, through=through, post=request.POST)
    chosen, errors = read_amounts(rows)
    context = _pay_context(request, filter_form, details, rows, token)
    if details.is_valid() and filters_ok and not errors:
        d = details.cleaned_data
        check_numbers = {int(k[6:]): v for k, v in request.POST.items() if k.startswith("check-") and k[6:].isdigit()}
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "These payments were already saved.")
                    return redirect(reverse("purchasing:payment-list"))
                payments = pay_bills(
                    date=d["date"], method=d["method"], paid_from=d["paid_from"], user=request.user,
                    lines=[PayLine(bill, amount) for bill, amount in chosen], check_numbers=check_numbers)
        except ERRORS as exc:
            return render(request, "purchasing/pay_bills.html", {**context, **common.error_context(exc)})
        numbers = ",".join(p.number for p in payments)
        return redirect(f"{reverse('purchasing:pay-done')}?n={numbers}")
    return render(request, "purchasing/pay_bills.html", {**context, "errors": errors})


@login_required
def pay_done(request):
    numbers = [n for n in request.GET.get("n", "").split(",") if n]
    payments = list(VendorPayment.objects.filter(number__in=numbers).select_related("vendor", "paid_from").order_by("number"))
    rows = [{"payment": p, "applications": list(p.applications.select_related("bill").order_by("id"))} for p in payments]
    total = sum((p.amount for p in payments), Decimal("0.00"))
    return render(request, "purchasing/pay_done.html", {"rows": rows, "total": total, "nav_active": "pay-bills"})


# ---- single payments ----

@login_required
def payment_list(request):
    payments = VendorPayment.objects.select_related("vendor", "paid_from")
    method = request.GET.get("method", "")
    if method in VendorPaymentMethod.values:
        payments = payments.filter(method=method)
    vendor = request.GET.get("vendor", "")
    if vendor.isdigit():
        payments = payments.filter(vendor_id=vendor)
    status = request.GET.get("status", "")
    if status in DocStatus.values:
        payments = payments.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        payments = payments.filter(
            Q(number__icontains=q) | Q(vendor__name__icontains=q) | Q(check_number__icontains=q) | Q(reference__icontains=q))
    page = Paginator(payments, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"payment": p, "unapplied": vendor_payment_unapplied(p)} for p in page]
    qs = "&".join(f"{k}={request.GET[k]}" for k in ("method", "vendor", "status", "q") if request.GET.get(k))
    return render(request, "purchasing/payment_list.html", {
        "rows": rows, "page": page, "method": method, "vendor": vendor, "status": status, "q": q, "querystring": qs,
        "vendors": Vendor.objects.all(), "methods": VendorPaymentMethod.choices, "nav_active": "pay-bills",
    })


def _new_context(form, token, vendor, rows, **extra):
    return {"form": form, "token": token, "vendor": vendor, "rows": rows, "pay_accounts": pay_from_accounts(),
            "nav_active": "pay-bills", **extra}


@login_required
def payment_new(request):
    if request.method != "POST":
        vendor = Vendor.objects.filter(pk=request.GET["vendor"]).first() if request.GET.get("vendor", "").isdigit() else None
        if vendor is None:
            return render(request, "purchasing/payment_new.html", _new_context(
                VendorPaymentForm(initial={"date": _today().isoformat()}), "", None, []))
        initial = {"vendor": vendor.pk, "date": _today().isoformat(), "method": "ACH"}
        return render(request, "purchasing/payment_new.html", _new_context(
            VendorPaymentForm(initial=initial), common.new_token(), vendor, open_bill_rows(vendor=vendor)))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This payment was already saved.")
        return redirect(reverse("purchasing:payment-list"))
    form = VendorPaymentForm(request.POST)
    vendor = Vendor.objects.filter(pk=request.POST["vendor"]).first() if request.POST.get("vendor", "").isdigit() else None
    if vendor is None:
        return render(request, "purchasing/payment_new.html", _new_context(form, token, None, []))
    rows = open_bill_rows(vendor=vendor, post=request.POST, prefix="apply")
    chosen, errors = read_amounts(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This payment was already saved.")
                    return redirect(reverse("purchasing:payment-list"))
                payment = pay_vendor(
                    vendor=vendor, date=d["date"], method=d["method"], amount=d["amount"], paid_from=d["paid_from"],
                    user=request.user, check_number=d["check_number"], reference=d["reference"], applications=chosen)
        except ERRORS as exc:
            return render(request, "purchasing/payment_new.html", _new_context(form, token, vendor, rows, **common.error_context(exc)))
        messages.success(request, f"Saved {payment.number}.")
        return redirect(reverse("purchasing:payment-detail", args=[payment.number]))
    return render(request, "purchasing/payment_new.html", _new_context(form, token, vendor, rows, errors=errors))


@login_required
def payment_detail(request, number):
    payment = payment_or_404(number)
    return render(request, "purchasing/payment_detail.html", {
        "payment": payment, "unapplied": vendor_payment_unapplied(payment),
        "applications": payment.applications.select_related("bill").order_by("applied_on", "id"),
        "is_void": payment.status == DocStatus.VOID, "is_admin": is_admin(request.user), "nav_active": "pay-bills",
    })


# ---- applying a payment or a credit to bills (also used by credits) ----

def apply_screen(request, doc, vendor, context, apply_one, unapplied):
    if request.method != "POST":
        rows = open_bill_rows(vendor=vendor, prefix="apply", fill=oldest_first(vendor, unapplied))
        form = ApplyForm(initial={"date": _today().isoformat()})
        return render(request, "purchasing/apply.html", {**context, "form": form, "rows": rows, "token": common.new_token()})
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "These applications were already saved.")
        return redirect(context["back"])
    form = ApplyForm(request.POST)
    rows = open_bill_rows(vendor=vendor, post=request.POST, prefix="apply")
    chosen, errors = read_amounts(rows)
    if not chosen and not errors:
        errors = ["Enter an amount for at least one bill."]
    page = {**context, "form": form, "rows": rows, "token": token}
    if form.is_valid() and not errors:
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "These applications were already saved.")
                    return redirect(context["back"])
                for bill, amount in chosen:
                    apply_one(bill, amount, form.cleaned_data["date"])
        except ERRORS as exc:
            return render(request, "purchasing/apply.html", {**page, **common.error_context(exc)})
        messages.success(request, "Applied.")
        return redirect(context["back"])
    return render(request, "purchasing/apply.html", {**page, "errors": errors})


@login_required
def payment_apply(request, number):
    payment = payment_or_404(number)
    unapplied = vendor_payment_unapplied(payment)
    back = reverse("purchasing:payment-detail", args=[number])
    context = {"doc": payment, "doc_kind": "payment", "unapplied": unapplied, "back": back, "nav_active": "pay-bills"}
    if payment.status == DocStatus.VOID:
        messages.error(request, f"{payment.number} is void.")
        return redirect(back)
    return apply_screen(request, payment, payment.vendor, context, lambda bill, amount, when: apply_vendor_payment(
        payment, bill, amount, date=when, user=request.user), unapplied)


def void_screen(request, doc, kind, service, back, nav):
    require_admin(request.user)
    context = {"doc": doc, "kind": kind, "back": back, "nav_active": nav}
    if request.method != "POST":
        return render(request, "purchasing/void_form.html", {**context, "form": VoidDocForm(initial={"date": doc.date.isoformat()})})
    form = VoidDocForm(request.POST)
    if form.is_valid():
        try:
            service(doc, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"])
        except ERRORS as exc:
            return render(request, "purchasing/void_form.html", {**context, "form": form, **common.error_context(exc)})
        messages.success(request, f"{doc.number} was voided.")
        return redirect(back)
    return render(request, "purchasing/void_form.html", {**context, "form": form})


@login_required
def payment_void(request, number):
    payment = payment_or_404(number)
    return void_screen(request, payment, "payment", void_vendor_payment,
                       reverse("purchasing:payment-detail", args=[number]), "pay-bills")

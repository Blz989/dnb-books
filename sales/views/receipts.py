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

from contacts.models import Customer
from core.roles import is_admin, require_admin
from items.errors import StockError
from ledger.errors import PostingError

from ..balances import credit_total, credit_unapplied, invoice_balance, payment_unapplied
from ..errors import SalesError
from ..forms import ApplyForm, CreditNoteForm, PaymentForm, RefundForm, VoidDocForm
from ..models import (
    CreditNote, DocStatus, Invoice, Payment, PaymentMethod, Refund, RefundMethod, SalesSettings,
)
from ..posting.receipts import (
    CreditLineSpec, apply_credit, apply_payment, create_credit_note, issue_refund, oldest_first, receive_payment,
    void_credit_note, void_payment, void_refund,
)
from . import common

PAGE_SIZE = 50
ERRORS = (SalesError, PostingError, StockError)
CREDIT_LINES = 8


def _today():
    return timezone.localdate()


def _or_404(model, number: str, label: str):
    doc = model.objects.filter(number=number).select_related("customer").first()
    if doc is None:
        raise Http404(f"No {label} has that number.")
    return doc


def _page(request, queryset):
    return Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))


def _qs(request, *keys):
    return "&".join(f"{k}={request.GET[k]}" for k in keys if request.GET.get(k))


# ---- shared: amounts against open invoices ----

def _invoice_rows(customer, post=None, fill=None):
    """Open invoices (oldest due first) with the amount typed, or an oldest-first fill."""
    rows = []
    for invoice in customer.invoices.filter(status=DocStatus.OPEN).order_by("due_date", "date", "id"):
        balance = invoice_balance(invoice)
        if balance <= 0:
            continue
        if post is not None:
            value = post.get(f"apply-{invoice.pk}", "").strip()
        else:
            value = f"{fill.get(invoice.pk, ''):.2f}" if invoice.pk in (fill or {}) else ""
        rows.append({"invoice": invoice, "balance": balance, "value": value})
    return rows


def _read_applications(rows):
    applications, errors = [], []
    for r in rows:
        if not r["value"]:
            continue
        try:
            amount = Decimal(r["value"].replace(",", ""))
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"{r['invoice'].number}: enter an amount like 25.00.")
            continue
        if amount != 0:
            applications.append((r["invoice"], amount))
    return applications, errors


def _fill_for(customer, amount):
    return {inv.pk: amt for inv, amt in oldest_first(customer, amount)}


# ---- payments ----

@login_required
def payment_list(request):
    payments = Payment.objects.select_related("customer", "deposit")
    method = request.GET.get("method", "")
    if method in PaymentMethod.values:
        payments = payments.filter(method=method)
    customer = request.GET.get("customer", "")
    if customer.isdigit():
        payments = payments.filter(customer_id=customer)
    status = request.GET.get("status", "")
    if status in DocStatus.values:
        payments = payments.filter(status=status)
    q = request.GET.get("q", "").strip()
    if q:
        payments = payments.filter(Q(number__icontains=q) | Q(customer__name__icontains=q) | Q(reference__icontains=q))
    page = _page(request, payments)
    rows = [{"payment": p, "unapplied": payment_unapplied(p)} for p in page]
    return render(request, "sales/payment_list.html", {
        "rows": rows, "page": page, "method": method, "customer": customer, "status": status, "q": q,
        "querystring": _qs(request, "method", "customer", "status", "q"), "customers": Customer.objects.all(),
        "methods": PaymentMethod.choices, "nav_active": "payments",
    })


def _payment_context(form, token, customer, rows, **extra):
    return {"form": form, "token": token, "customer": customer, "rows": rows, "nav_active": "payments", **extra}


@login_required
def payment_new(request):
    if request.method != "POST":
        customer = Customer.objects.filter(pk=request.GET["customer"]).first() if request.GET.get("customer", "").isdigit() else None
        if customer is None:
            form = PaymentForm(initial={"date": _today().isoformat()})
            return render(request, "sales/payment_new.html", _payment_context(form, "", None, []))
        initial = {"customer": customer.pk, "date": _today().isoformat(), "method": PaymentMethod.CHECK}
        fill = None
        invoice = Invoice.objects.filter(number=request.GET.get("invoice", ""), customer=customer).first()
        if invoice is not None and invoice.status == DocStatus.OPEN:
            balance = invoice_balance(invoice)
            initial["amount"] = f"{balance:.2f}"
            fill = {invoice.pk: balance}
        form = PaymentForm(initial=initial)
        return render(request, "sales/payment_new.html", _payment_context(
            form, common.new_token(), customer, _invoice_rows(customer, fill=fill)))

    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This payment was already saved.")
        return redirect(reverse("sales:payment-list"))
    form = PaymentForm(request.POST)
    customer = Customer.objects.filter(pk=request.POST.get("customer", "")).first() if request.POST.get("customer", "").isdigit() else None
    if customer is None:
        return render(request, "sales/payment_new.html", _payment_context(form, token, None, []))
    if request.POST.get("fill") == "oldest":
        try:
            amount = Decimal(request.POST.get("amount", "0").replace(",", ""))
        except InvalidOperation:
            amount = Decimal("0")
        rows = _invoice_rows(customer, fill=_fill_for(customer, amount))
        return render(request, "sales/payment_new.html", _payment_context(form, token, customer, rows))
    rows = _invoice_rows(customer, post=request.POST)
    applications, errors = _read_applications(rows)
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This payment was already saved.")
                    return redirect(reverse("sales:payment-list"))
                payment = receive_payment(
                    customer=customer, date=d["date"], method=d["method"], amount=d["amount"], user=request.user,
                    reference=d["reference"], bank_account=d["bank_account"], applications=applications)
        except ERRORS as exc:
            return render(request, "sales/payment_new.html", _payment_context(form, token, customer, rows, **common.error_context(exc)))
        messages.success(request, f"Saved {payment.number}.")
        return redirect(reverse("sales:payment-detail", args=[payment.number]))
    return render(request, "sales/payment_new.html", _payment_context(form, token, customer, rows, errors=errors))


@login_required
def payment_detail(request, number):
    payment = _or_404(Payment, number, "payment")
    unapplied = payment_unapplied(payment)
    return render(request, "sales/payment_detail.html", {
        "payment": payment, "unapplied": unapplied,
        "applications": payment.applications.select_related("invoice").order_by("applied_on", "id"),
        "refunds": payment.refunds.all(), "is_void": payment.status == DocStatus.VOID,
        "is_admin": is_admin(request.user), "nav_active": "payments",
    })


@login_required
def payment_apply(request, number):
    payment = _or_404(Payment, number, "payment")
    unapplied = payment_unapplied(payment)
    context = {"doc": payment, "doc_kind": "payment", "unapplied": unapplied, "back": reverse("sales:payment-detail", args=[number]),
               "nav_active": "payments"}
    if payment.status == DocStatus.VOID:
        messages.error(request, f"{payment.number} is void.")
        return redirect(context["back"])
    return _apply_screen(request, payment.customer, context, lambda inv, amt, d: apply_payment(
        payment, inv, amt, date=d, user=request.user), unapplied)


@login_required
def payment_void(request, number):
    payment = _or_404(Payment, number, "payment")
    return _void_screen(request, payment, "payment", void_payment, reverse("sales:payment-detail", args=[number]), "payments")


# ---- apply (payments and credit notes) ----

def _apply_screen(request, customer, context, apply_one, unapplied):
    if request.method != "POST":
        rows = _invoice_rows(customer, fill=_fill_for(customer, unapplied))
        form = ApplyForm(initial={"date": _today().isoformat()})
        return render(request, "sales/apply.html", {**context, "form": form, "rows": rows, "token": common.new_token()})
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "These applications were already saved.")
        return redirect(context["back"])
    form = ApplyForm(request.POST)
    rows = _invoice_rows(customer, post=request.POST)
    applications, errors = _read_applications(rows)
    if not applications and not errors:
        errors = ["Enter an amount for at least one invoice."]
    page = {**context, "form": form, "rows": rows, "token": token}
    if form.is_valid() and not errors:
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "These applications were already saved.")
                    return redirect(context["back"])
                for invoice, amount in applications:
                    apply_one(invoice, amount, form.cleaned_data["date"])
        except ERRORS as exc:
            return render(request, "sales/apply.html", {**page, **common.error_context(exc)})
        messages.success(request, "Applied.")
        return redirect(context["back"])
    return render(request, "sales/apply.html", {**page, "errors": errors})


# ---- generic void screen ----

def _void_screen(request, doc, kind, service, back, nav):
    require_admin(request.user)
    context = {"doc": doc, "kind": kind, "back": back, "nav_active": nav}
    if request.method != "POST":
        form = VoidDocForm(initial={"date": doc.date.isoformat()})
        return render(request, "sales/void_form.html", {**context, "form": form})
    form = VoidDocForm(request.POST)
    if form.is_valid():
        try:
            service(doc, user=request.user, reason=form.cleaned_data["reason"], date=form.cleaned_data["date"])
        except ERRORS as exc:
            return render(request, "sales/void_form.html", {**context, "form": form, **common.error_context(exc)})
        messages.success(request, f"{doc.number} was voided.")
        return redirect(back)
    return render(request, "sales/void_form.html", {**context, "form": form})


# ---- credit notes ----

@login_required
def credit_list(request):
    notes = CreditNote.objects.select_related("customer")
    customer = request.GET.get("customer", "")
    if customer.isdigit():
        notes = notes.filter(customer_id=customer)
    q = request.GET.get("q", "").strip()
    if q:
        notes = notes.filter(Q(number__icontains=q) | Q(customer__name__icontains=q))
    page = _page(request, notes)
    rows = [{"note": n, "total": credit_total(n), "unapplied": credit_unapplied(n)} for n in page]
    return render(request, "sales/credit_list.html", {
        "rows": rows, "page": page, "customer": customer, "q": q, "querystring": _qs(request, "customer", "q"),
        "customers": Customer.objects.all(), "nav_active": "credits",
    })


def _credit_rows(post=None):
    rows = []
    for i in range(CREDIT_LINES):
        rows.append({
            "index": i, "description": post.get(f"lines-{i}-description", "").strip() if post else "",
            "amount": post.get(f"lines-{i}-amount", "").strip() if post else "",
            "account": post.get(f"lines-{i}-account", "") if post else "",
        })
    return rows


def _credit_context(form, rows, token, **extra):
    from ledger.models import Account, AccountType

    accounts = Account.objects.filter(is_active=True, type__in=[AccountType.INCOME, AccountType.EXPENSE]).order_by("number")
    shown = max([r["index"] for r in rows if r["description"] or r["amount"]] + [1]) + 1
    return {"form": form, "rows": rows, "token": token, "accounts": accounts, "shown": shown,
            "default_account": SalesSettings.load().returns_account, "nav_active": "credits", **extra}


@login_required
def credit_new(request):
    if request.method != "POST":
        initial = {"date": _today().isoformat()}
        if request.GET.get("customer", "").isdigit():
            initial["customer"] = request.GET["customer"]
        return render(request, "sales/credit_form.html", _credit_context(CreditNoteForm(initial=initial), _credit_rows(), common.new_token()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This credit note was already saved.")
        return redirect(reverse("sales:credit-list"))
    form = CreditNoteForm(request.POST)
    rows = _credit_rows(request.POST)
    lines, errors = [], []
    from ledger.models import Account

    n = 0
    for r in rows:
        if not r["description"] and not r["amount"]:
            continue
        n += 1
        try:
            amount = Decimal(r["amount"].replace(",", ""))
            if not amount.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"Line {n}: enter an amount above zero.")
            continue
        account = None
        if r["account"]:
            account = Account.objects.filter(pk=r["account"]).first() if r["account"].isdigit() else None
            if account is None:
                errors.append(f"Line {n}: choose an account from the list.")
                continue
        lines.append(CreditLineSpec(r["description"], amount, account))
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This credit note was already saved.")
                    return redirect(reverse("sales:credit-list"))
                note = create_credit_note(customer=d["customer"], date=d["date"], lines=lines, reason=d["reason"], user=request.user)
        except ERRORS as exc:
            return render(request, "sales/credit_form.html", _credit_context(form, rows, token, **common.error_context(exc)))
        messages.success(request, f"Saved {note.number}.")
        return redirect(reverse("sales:credit-detail", args=[note.number]))
    return render(request, "sales/credit_form.html", _credit_context(form, rows, token, errors=errors))


@login_required
def credit_detail(request, number):
    note = _or_404(CreditNote, number, "credit note")
    return render(request, "sales/credit_detail.html", {
        "note": note, "lines": note.lines.select_related("account"), "total": credit_total(note),
        "unapplied": credit_unapplied(note),
        "applications": note.applications.select_related("invoice").order_by("applied_on", "id"),
        "refunds": note.refunds.all(), "is_void": note.status == DocStatus.VOID,
        "is_admin": is_admin(request.user), "nav_active": "credits",
    })


@login_required
def credit_apply(request, number):
    note = _or_404(CreditNote, number, "credit note")
    unapplied = credit_unapplied(note)
    context = {"doc": note, "doc_kind": "credit note", "unapplied": unapplied,
               "back": reverse("sales:credit-detail", args=[number]), "nav_active": "credits"}
    if note.status == DocStatus.VOID:
        messages.error(request, f"{note.number} is void.")
        return redirect(context["back"])
    return _apply_screen(request, note.customer, context, lambda inv, amt, d: apply_credit(
        note, inv, amt, date=d, user=request.user), unapplied)


@login_required
def credit_void(request, number):
    note = _or_404(CreditNote, number, "credit note")
    return _void_screen(request, note, "credit note", void_credit_note, reverse("sales:credit-detail", args=[number]), "credits")


# ---- refunds (admin only) ----

@login_required
def refund_new(request):
    require_admin(request.user)
    source = None
    if request.GET.get("payment"):
        source = Payment.objects.filter(number=request.GET["payment"]).select_related("customer").first()
    elif request.GET.get("credit"):
        source = CreditNote.objects.filter(number=request.GET["credit"]).select_related("customer").first()
    if request.method == "POST":
        key, number = ("payment", request.POST.get("source_payment")) if request.POST.get("source_payment") else ("credit", request.POST.get("source_credit"))
        model = Payment if key == "payment" else CreditNote
        source = model.objects.filter(number=number).select_related("customer").first()
    if source is None:
        raise Http404("Choose a payment or credit note to refund.")
    available = payment_unapplied(source) if isinstance(source, Payment) else credit_unapplied(source)
    back = reverse("sales:payment-detail" if isinstance(source, Payment) else "sales:credit-detail", args=[source.number])
    context = {"source": source, "is_payment": isinstance(source, Payment), "available": available, "back": back, "nav_active": "payments"}
    if request.method != "POST":
        form = RefundForm(initial={"date": _today().isoformat(), "method": RefundMethod.CHECK, "amount": f"{max(available, 0):.2f}"})
        return render(request, "sales/refund_form.html", {**context, "form": form, "token": common.new_token()})
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This refund was already saved.")
        return redirect(back)
    form = RefundForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This refund was already saved.")
                    return redirect(back)
                refund = issue_refund(
                    source=source, date=d["date"], bank_account=d["bank_account"], method=d["method"], amount=d["amount"],
                    user=request.user, reference=d["reference"])
        except ERRORS as exc:
            return render(request, "sales/refund_form.html", {**context, "form": form, "token": token, **common.error_context(exc)})
        messages.success(request, f"Saved {refund.number}.")
        return redirect(reverse("sales:refund-detail", args=[refund.number]))
    return render(request, "sales/refund_form.html", {**context, "form": form, "token": token})


@login_required
def refund_detail(request, number):
    refund = _or_404(Refund, number, "refund")
    return render(request, "sales/refund_detail.html", {
        "refund": refund, "is_void": refund.status == DocStatus.VOID, "is_admin": is_admin(request.user), "nav_active": "payments"})


@login_required
def refund_void(request, number):
    refund = _or_404(Refund, number, "refund")
    return _void_screen(request, refund, "refund", void_refund, reverse("sales:refund-detail", args=[number]), "payments")

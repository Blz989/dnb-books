from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.roles import is_admin
from sales import reports
from sales.balances import credit_unapplied, customer_balance, payment_unapplied
from sales.models import DocStatus

from .forms import CustomerForm
from .models import Customer

PAGE_SIZE = 50


def _posted(request):
    return request.POST if request.method == "POST" else None


@login_required
def customer_list(request):
    customers = Customer.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        customers = customers.filter(Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q))
    show_inactive = request.GET.get("inactive") == "1"
    if not show_inactive:
        customers = customers.filter(is_active=True)
    page = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [
        {
            "customer": c,
            "balance": customer_balance(c),
            "overdue": bool(reports.open_invoices(customer=c, overdue_only=True)),
        }
        for c in page
    ]
    querystring = "&".join(f"{k}={v}" for k, v in (("q", q), ("inactive", "1" if show_inactive else "")) if v)
    return render(request, "contacts/customer_list.html", {
        "rows": rows, "page": page, "q": q, "show_inactive": show_inactive, "querystring": querystring,
        "nav_active": "customers",
    })


def _customer_or_404(pk) -> Customer:
    customer = Customer.objects.filter(pk=pk).first()
    if customer is None:
        raise Http404("No customer has that id.")
    return customer


def _history(customer: Customer) -> list[dict]:
    rows = []
    for inv in customer.invoices.all():
        rows.append({"date": inv.date, "kind": "Invoice", "number": inv.number, "void": inv.status == DocStatus.VOID,
                     "url": f"/sales/invoices/{inv.number}/", "sign": 1,
                     "amount": sum((l.amount for l in inv.lines.all()), 0)})
    for pay in customer.payments.all():
        rows.append({"date": pay.date, "kind": f"Payment ({pay.get_method_display()})", "number": pay.number,
                     "void": pay.status == DocStatus.VOID, "url": f"/sales/payments/{pay.number}/", "sign": -1,
                     "amount": pay.amount})
    for note in customer.credit_notes.all():
        rows.append({"date": note.date, "kind": "Credit note", "number": note.number,
                     "void": note.status == DocStatus.VOID, "url": f"/sales/credits/{note.number}/", "sign": -1,
                     "amount": sum((l.amount for l in note.lines.all()), 0)})
    for ref in customer.refunds.all():
        rows.append({"date": ref.date, "kind": "Refund", "number": ref.number, "void": ref.status == DocStatus.VOID,
                     "url": f"/sales/refunds/{ref.number}/", "sign": 1, "amount": ref.amount})
    return sorted(rows, key=lambda r: (r["date"], r["number"]), reverse=True)


@login_required
def customer_detail(request, pk):
    customer = _customer_or_404(pk)
    credits = []
    for pay in customer.payments.filter(status=DocStatus.OPEN):
        left = payment_unapplied(pay)
        if left > 0:
            credits.append({"kind": "Payment", "doc": pay, "unapplied": left, "path": "payments"})
    for note in customer.credit_notes.filter(status=DocStatus.OPEN):
        left = credit_unapplied(note)
        if left > 0:
            credits.append({"kind": "Credit note", "doc": note, "unapplied": left, "path": "credits"})
    open_rows = reports.open_invoices(customer=customer)
    return render(request, "contacts/customer_detail.html", {
        "customer": customer, "balance": customer_balance(customer), "open_rows": open_rows,
        "credits": credits, "history": _history(customer)[:25], "nav_active": "customers", "is_admin": is_admin(request.user),
    })


@login_required
@permission_required("contacts.add_customer", raise_exception=True)
def customer_new(request):
    form = CustomerForm(_posted(request))
    if request.method == "POST" and form.is_valid():
        customer = form.save()
        messages.success(request, f"Added {customer.name}.")
        return redirect(reverse("contacts:detail", args=[customer.pk]))
    return render(request, "contacts/customer_form.html", {"form": form, "nav_active": "customers"})


@login_required
@permission_required("contacts.change_customer", raise_exception=True)
def customer_edit(request, pk):
    customer = _customer_or_404(pk)
    form = CustomerForm(_posted(request), instance=customer)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {customer.name}.")
        return redirect(reverse("contacts:detail", args=[customer.pk]))
    return render(request, "contacts/customer_form.html", {"form": form, "customer": customer, "nav_active": "customers"})


@login_required
@permission_required("contacts.change_customer", raise_exception=True)
@require_POST
def customer_deactivate(request, pk):
    customer = _customer_or_404(pk)
    balance = customer_balance(customer)
    if balance != 0:
        messages.error(request, f"{customer.name} has a balance of {balance:,.2f}. Settle it before deactivating.")
    else:
        customer.is_active = False
        customer.save()
        messages.success(request, f"{customer.name} is now inactive.")
    return redirect(reverse("contacts:detail", args=[customer.pk]))

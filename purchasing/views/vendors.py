from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contacts.models import Vendor
from sales.models import DocStatus

from ..balances import (
    bill_balance, bills_with_balances, vendor_balance, vendor_credit_unapplied, vendor_payment_unapplied,
)
from ..forms import VendorForm

PAGE_SIZE = 50


def _posted(request):
    return request.POST if request.method == "POST" else None


def _vendor_or_404(pk) -> Vendor:
    vendor = Vendor.objects.filter(pk=pk).first()
    if vendor is None:
        raise Http404("No vendor has that id.")
    return vendor


def _open_bills(vendor: Vendor, today):
    rows = []
    for bill in bills_with_balances().filter(vendor=vendor, status=DocStatus.OPEN).order_by("due_date", "date", "id"):
        balance = bill_balance(bill)
        if balance > 0:
            rows.append({"bill": bill, "balance": balance, "days_overdue": (today - bill.due_date).days})
    return rows


@login_required
def vendor_list(request):
    vendors = Vendor.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        vendors = vendors.filter(Q(name__icontains=q) | Q(contact_name__icontains=q) | Q(email__icontains=q))
    show_inactive = request.GET.get("inactive") == "1"
    if not show_inactive:
        vendors = vendors.filter(is_active=True)
    page = Paginator(vendors, PAGE_SIZE).get_page(request.GET.get("page"))
    today = timezone.localdate()
    rows = [
        {"vendor": v, "balance": vendor_balance(v), "overdue": any(r["days_overdue"] > 0 for r in _open_bills(v, today))}
        for v in page
    ]
    querystring = "&".join(f"{k}={v}" for k, v in (("q", q), ("inactive", "1" if show_inactive else "")) if v)
    return render(request, "purchasing/vendor_list.html", {
        "rows": rows, "page": page, "q": q, "show_inactive": show_inactive, "querystring": querystring,
        "nav_active": "vendors",
    })


def _history(vendor: Vendor) -> list[dict]:
    rows = []
    for bill in vendor.bills.prefetch_related("item_lines", "expense_lines"):
        total = sum((l.amount for l in bill.item_lines.all()), 0) + sum((l.amount for l in bill.expense_lines.all()), 0)
        rows.append({"date": bill.date, "kind": "Bill", "number": bill.number, "void": bill.status == DocStatus.VOID,
                     "url": f"/purchasing/bills/{bill.number}/", "amount": total})
    for pay in vendor.payments.all():
        rows.append({"date": pay.date, "kind": f"Payment ({pay.get_method_display()})", "number": pay.number,
                     "void": pay.status == DocStatus.VOID, "url": f"/purchasing/payments/{pay.number}/",
                     "amount": pay.amount})
    for credit in vendor.credits.prefetch_related("lines"):
        rows.append({"date": credit.date, "kind": "Vendor credit", "number": credit.number,
                     "void": credit.status == DocStatus.VOID, "url": f"/purchasing/credits/{credit.number}/",
                     "amount": sum((l.amount for l in credit.lines.all()), 0)})
    return sorted(rows, key=lambda r: (r["date"], r["number"]), reverse=True)


@login_required
def vendor_detail(request, pk):
    vendor = _vendor_or_404(pk)
    credits = []
    for pay in vendor.payments.filter(status=DocStatus.OPEN):
        left = vendor_payment_unapplied(pay)
        if left > 0:
            credits.append({"kind": "Payment", "doc": pay, "unapplied": left, "path": "payments"})
    for credit in vendor.credits.filter(status=DocStatus.OPEN):
        left = vendor_credit_unapplied(credit)
        if left > 0:
            credits.append({"kind": "Vendor credit", "doc": credit, "unapplied": left, "path": "credits"})
    open_rows = _open_bills(vendor, timezone.localdate())
    return render(request, "purchasing/vendor_detail.html", {
        "vendor": vendor, "balance": vendor_balance(vendor), "open_rows": open_rows, "credits": credits,
        "history": _history(vendor)[:25], "nav_active": "vendors",
    })


@login_required
@permission_required("contacts.add_vendor", raise_exception=True)
def vendor_new(request):
    form = VendorForm(_posted(request), user=request.user)
    if request.method == "POST" and form.is_valid():
        vendor = form.save()
        messages.success(request, f"Added {vendor.name}.")
        return redirect(reverse("vendors:detail", args=[vendor.pk]))
    return render(request, "purchasing/vendor_form.html", {"form": form, "nav_active": "vendors"})


@login_required
@permission_required("contacts.change_vendor", raise_exception=True)
def vendor_edit(request, pk):
    vendor = _vendor_or_404(pk)
    form = VendorForm(_posted(request), instance=vendor, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {vendor.name}.")
        return redirect(reverse("vendors:detail", args=[vendor.pk]))
    return render(request, "purchasing/vendor_form.html", {"form": form, "vendor": vendor, "nav_active": "vendors"})


@login_required
@permission_required("contacts.change_vendor", raise_exception=True)
@require_POST
def vendor_deactivate(request, pk):
    vendor = _vendor_or_404(pk)
    balance = vendor_balance(vendor)
    if balance != 0:
        messages.error(request, f"{vendor.name} has a balance of {balance:,.2f}. Settle it before deactivating.")
    else:
        vendor.is_active = False
        vendor.save()
        messages.success(request, f"{vendor.name} is now inactive.")
    return redirect(reverse("vendors:detail", args=[vendor.pk]))

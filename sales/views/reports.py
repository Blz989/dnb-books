from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from contacts.models import Customer
from ledger.forms import AsOfForm, date_field
from ledger.views import _data, _wants_csv

from .. import csv_export, reports


def _today():
    return timezone.localdate()


class OpenInvoicesForm(forms.Form):
    as_of = date_field("As of")
    customer = forms.ModelChoiceField(Customer.objects.all(), required=False, empty_label="All customers")
    overdue = forms.BooleanField(required=False, label="Overdue only")


@login_required
def ar_aging(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.ar_aging(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.ar_aging_csv(report)
    return render(request, "sales/report_ar_aging.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def open_invoices(request):
    form = OpenInvoicesForm(_data(request, {"as_of": _today().isoformat()}))
    rows = None
    if form.is_valid():
        d = form.cleaned_data
        rows = reports.open_invoices(d["as_of"], d["customer"], d["overdue"])
        if _wants_csv(request):
            return csv_export.open_invoices_csv(rows)
    total = sum((r.balance for r in rows), 0) if rows is not None else None
    return render(request, "sales/report_open_invoices.html", {
        "form": form, "report": rows is not None, "rows": rows, "total": total, "nav_active": "reports"})


@login_required
def customer_balances(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    rows = reports.customer_balances(form.cleaned_data["as_of"]) if form.is_valid() else None
    if rows is not None and _wants_csv(request):
        return csv_export.customer_balances_csv(rows)
    total = sum((r.balance for r in rows), 0) if rows is not None else None
    return render(request, "sales/report_customer_balances.html", {
        "form": form, "report": rows is not None, "rows": rows, "total": total, "nav_active": "reports"})


@login_required
def open_sales_orders(request):
    rows = reports.open_sales_orders()
    if _wants_csv(request):
        return csv_export.open_sales_orders_csv(rows)
    return render(request, "sales/report_open_sales_orders.html", {"rows": rows, "report": True, "nav_active": "reports"})


@login_required
def undeposited_funds(request):
    report = reports.undeposited_funds()
    if _wants_csv(request):
        return csv_export.undeposited_funds_csv(report)
    return render(request, "sales/report_undeposited_funds.html", {"report": report, "nav_active": "reports"})


@login_required
def drop_ship_awaiting_bill(request):
    lines = reports.drop_ship_awaiting_bill()
    if _wants_csv(request):
        return csv_export.drop_ship_csv(lines)
    return render(request, "sales/report_drop_ship.html", {"lines": lines, "report": True, "nav_active": "reports"})

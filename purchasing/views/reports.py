from datetime import timedelta

from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from core.roles import is_admin, require_admin
from ledger.forms import AsOfForm, date_field
from ledger.views import _data, _wants_csv

from .. import csv_export, reports


def _today():
    return timezone.localdate()


class ThroughForm(forms.Form):
    through = date_field("Due through")


class YearForm(forms.Form):
    year = forms.IntegerField(
        label="Year", min_value=2000, max_value=2100,
        error_messages={"invalid": "Enter a year like 2026.", "required": "Enter a year like 2026.",
                        "min_value": "Enter a year like 2026.", "max_value": "Enter a year like 2026."})


@login_required
def ap_aging(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.ap_aging(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.ap_aging_csv(report)
    return render(request, "purchasing/report_ap_aging.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def bills_due(request):
    form = ThroughForm(_data(request, {"through": (_today() + timedelta(days=7)).isoformat()}))
    rows = reports.bills_due(form.cleaned_data["through"], _today()) if form.is_valid() else None
    if rows is not None and _wants_csv(request):
        return csv_export.bills_due_csv(rows)
    total = sum((r.balance for r in rows), 0) if rows is not None else None
    return render(request, "purchasing/report_bills_due.html", {
        "form": form, "report": rows is not None, "rows": rows, "total": total, "nav_active": "reports"})


@login_required
def vendor_balances(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    rows = reports.vendor_balances(form.cleaned_data["as_of"]) if form.is_valid() else None
    if rows is not None and _wants_csv(request):
        return csv_export.vendor_balances_csv(rows)
    total = sum((r.balance for r in rows), 0) if rows is not None else None
    return render(request, "purchasing/report_vendor_balances.html", {
        "form": form, "report": rows is not None, "rows": rows, "total": total, "nav_active": "reports"})


@login_required
def open_purchase_orders(request):
    rows = reports.open_purchase_orders()
    if _wants_csv(request):
        return csv_export.open_purchase_orders_csv(rows)
    return render(request, "purchasing/report_open_purchase_orders.html", {
        "rows": rows, "report": True, "nav_active": "reports"})


@login_required
def summary_1099(request):
    form = YearForm(_data(request, {"year": str(_today().year)}))
    summary = reports.summary_1099(form.cleaned_data["year"]) if form.is_valid() else None
    if summary and _wants_csv(request):
        require_admin(request.user)
        return csv_export.summary_1099_csv(summary)
    return render(request, "purchasing/report_1099.html", {
        "form": form, "report": summary, "csv_allowed": is_admin(request.user), "nav_active": "reports"})

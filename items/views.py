from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from ledger.forms import AsOfForm
from ledger.views import _data, _today, _wants_csv

from . import csv_export, reports
from .forms import MovementsForm


# ---- reports ----

@login_required
def inventory_valuation(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.inventory_valuation(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.valuation_csv(report)
    return render(request, "items/inventory_valuation.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def stock_movements(request):
    today = _today()
    form = MovementsForm(_data(request, {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}))
    rows = None
    if form.is_valid():
        d = form.cleaned_data
        rows = reports.stock_movements(d["start"], d["end"], d["item"])
        if _wants_csv(request):
            return csv_export.movements_csv(rows, d["start"], d["end"])
    return render(request, "items/stock_movements.html", {"form": form, "rows": rows, "nav_active": "reports"})


@login_required
def negative_stock(request):
    rows = reports.negative_stock()
    if _wants_csv(request):
        return csv_export.negative_stock_csv(rows)
    return render(request, "items/negative_stock.html", {"rows": rows, "nav_active": "reports"})

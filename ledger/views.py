from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import csv_export, reports
from .forms import AsOfForm, GeneralLedgerForm, RangeForm


def _data(request, defaults):
    """GET values over defaults; blank values fall back to the default."""
    data = dict(defaults)
    data.update({k: v for k, v in request.GET.items() if v != ""})
    return data


def _today():
    return timezone.localdate()


def _wants_csv(request):
    return request.GET.get("format") == "csv"


@login_required
def trial_balance(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.trial_balance(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.trial_balance_csv(report)
    return render(request, "ledger/trial_balance.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def profit_and_loss(request):
    today = _today()
    form = RangeForm(_data(request, {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}))
    report = reports.profit_and_loss(form.cleaned_data["start"], form.cleaned_data["end"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.profit_and_loss_csv(report)
    return render(request, "ledger/profit_and_loss.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def balance_sheet(request):
    form = AsOfForm(_data(request, {"as_of": _today().isoformat()}))
    report = reports.balance_sheet(form.cleaned_data["as_of"]) if form.is_valid() else None
    if report and _wants_csv(request):
        return csv_export.balance_sheet_csv(report)
    return render(request, "ledger/balance_sheet.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def general_ledger(request):
    today = _today()
    defaults = {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}
    if "account" not in request.GET:
        # First visit: show the picker without complaining about the missing account.
        form = GeneralLedgerForm(initial=defaults)
        return render(request, "ledger/general_ledger.html", {"form": form, "report": None, "nav_active": "reports"})
    form = GeneralLedgerForm(_data(request, defaults))
    report = None
    if form.is_valid():
        d = form.cleaned_data
        report = reports.general_ledger(d["account"], d["start"], d["end"])
        if _wants_csv(request):
            return csv_export.general_ledger_csv(report)
    return render(request, "ledger/general_ledger.html", {"form": form, "report": report, "nav_active": "reports"})

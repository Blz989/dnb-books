from datetime import date

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import csv_export, reports
from . import comparison
from .cash_basis import profit_and_loss_cash
from .forms import AsOfCompareForm, GeneralLedgerForm, ProfitAndLossForm


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
def index(request):
    from core.report_catalog import featured

    return render(request, "ledger/index.html", {"nav_active": "reports", "featured": featured()})


@login_required
def trial_balance(request):
    form = AsOfCompareForm(_data(request, {"as_of": _today().isoformat()}))
    report = compared = None
    if form.is_valid():
        as_of, years = form.cleaned_data["as_of"], form.cleaned_data["compare"]
        report = reports.trial_balance(as_of)
        if years:
            compared = comparison.trial_balance_comparison(as_of, years)
    if compared and _wants_csv(request):
        return comparison.comparison_csv(compared, f"trial-balance-comparison-{report.as_of.isoformat()}.csv")
    if report and _wants_csv(request):
        return csv_export.trial_balance_csv(report)
    return render(request, "ledger/trial_balance.html", {
        "form": form, "report": report, "comparison": compared, "nav_active": "reports"})


@login_required
def profit_and_loss(request):
    today = _today()
    form = ProfitAndLossForm(_data(request, {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}))
    report = compared = None
    basis = "accrual"
    if form.is_valid():
        start, end, years = form.cleaned_data["start"], form.cleaned_data["end"], form.cleaned_data["compare"]
        basis = form.cleaned_data["basis"]
        report = profit_and_loss_cash(start, end) if basis == "cash" else reports.profit_and_loss(start, end)
        if years:
            compared = comparison.profit_and_loss_comparison(start, end, years, basis)
    suffix = "-cash" if basis == "cash" else ""
    if compared and _wants_csv(request):
        return comparison.comparison_csv(
            compared, f"profit-and-loss{suffix}-comparison-{report.start.isoformat()}_{report.end.isoformat()}.csv")
    if report and _wants_csv(request):
        response = csv_export.profit_and_loss_csv(report)
        if suffix:
            response["Content-Disposition"] = response["Content-Disposition"].replace("profit-and-loss-", "profit-and-loss-cash-")
        return response
    return render(request, "ledger/profit_and_loss.html", {
        "form": form, "report": report, "comparison": compared, "basis": basis, "nav_active": "reports"})


@login_required
def balance_sheet(request):
    form = AsOfCompareForm(_data(request, {"as_of": _today().isoformat()}))
    report = compared = None
    if form.is_valid():
        as_of, years = form.cleaned_data["as_of"], form.cleaned_data["compare"]
        report = reports.balance_sheet(as_of)
        if years:
            compared = comparison.balance_sheet_comparison(as_of, years)
    if compared and _wants_csv(request):
        return comparison.comparison_csv(compared, f"balance-sheet-comparison-{report.as_of.isoformat()}.csv")
    if report and _wants_csv(request):
        return csv_export.balance_sheet_csv(report)
    return render(request, "ledger/balance_sheet.html", {
        "form": form, "report": report, "comparison": compared, "nav_active": "reports"})


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

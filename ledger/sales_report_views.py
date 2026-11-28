from datetime import date

from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from contacts.models import Customer, Vendor

from . import sales_reports
from .csv_export import amt, csv_response
from .forms import RangeForm
from .views import _data, _today, _wants_csv


def _defaults():
    today = _today()
    return {"start": date(today.year, today.month, 1).isoformat(), "end": today.isoformat()}


class CustomerRangeForm(RangeForm):
    customer = forms.ModelChoiceField(Customer.objects.all(), required=False, empty_label="All customers")


class VendorRangeForm(RangeForm):
    vendor = forms.ModelChoiceField(Vendor.objects.all(), required=False, empty_label="All vendors")


def _suffix(r):
    return f"{r.start.isoformat()}_{r.end.isoformat()}.csv"


def _opt(value):
    return "" if value is None else amt(value)


@login_required
def sales_by_customer(request):
    form = RangeForm(_data(request, _defaults()))
    report = sales_reports.sales_by_customer(form.cleaned_data["start"], form.cleaned_data["end"]) if form.is_valid() else None
    if report and _wants_csv(request):
        rows = [[r.customer.name, r.invoices, amt(r.sales), amt(r.credits), amt(r.net), amt(r.written_off)]
                for r in report.rows]
        t = report.total
        rows.append(["Total", t.invoices, amt(t.sales), amt(t.credits), amt(t.net), amt(t.written_off)])
        return csv_response(f"sales-by-customer-{_suffix(report)}",
                            ["Customer", "Invoices", "Sales", "Credits", "Net sales", "Written off"], rows)
    return render(request, "ledger/sales_by_customer.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def sales_by_item(request):
    form = CustomerRangeForm(_data(request, _defaults()))
    report = None
    if form.is_valid():
        d = form.cleaned_data
        report = sales_reports.sales_by_item(d["start"], d["end"], customer=d["customer"])
        if _wants_csv(request):
            rows = [[r.item.sku, r.item.name, r.quantity, amt(r.amount), _opt(r.average)] for r in report.rows]
            rows.append(["", "Total", "", amt(report.total), ""])
            return csv_response(f"sales-by-item-{_suffix(report)}",
                                ["SKU", "Item", "Quantity", "Sales", "Average price"], rows)
    return render(request, "ledger/sales_by_item.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def purchases_by_vendor(request):
    form = RangeForm(_data(request, _defaults()))
    report = sales_reports.purchases_by_vendor(form.cleaned_data["start"], form.cleaned_data["end"]) if form.is_valid() else None
    if report and _wants_csv(request):
        rows = [[r.vendor.name, r.bills, amt(r.purchases), amt(r.credits), amt(r.net)] for r in report.rows]
        t = report.total
        rows.append(["Total", t.bills, amt(t.purchases), amt(t.credits), amt(t.net)])
        return csv_response(f"purchases-by-vendor-{_suffix(report)}",
                            ["Vendor", "Bills", "Purchases", "Vendor credits", "Net purchases"], rows)
    return render(request, "ledger/purchases_by_vendor.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def purchases_by_item(request):
    form = VendorRangeForm(_data(request, _defaults()))
    report = None
    if form.is_valid():
        d = form.cleaned_data
        report = sales_reports.purchases_by_item(d["start"], d["end"], vendor=d["vendor"])
        if _wants_csv(request):
            rows = []
            for r in report.rows:
                if r.item:
                    rows.append([r.item.sku, r.item.name, r.quantity, amt(r.amount), _opt(r.average)])
                else:
                    rows.append([r.account.number, r.account.name, "", amt(r.amount), ""])
            rows.append(["", "Total", "", amt(report.total), ""])
            return csv_response(f"purchases-by-item-{_suffix(report)}",
                                ["SKU or account", "Name", "Quantity", "Amount", "Average cost"], rows)
    return render(request, "ledger/purchases_by_item.html", {"form": form, "report": report, "nav_active": "reports"})


@login_required
def cash_flow_view(request):
    from .cash_flow import cash_flow

    today = _today()
    form = RangeForm(_data(request, {"start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}))
    report = cash_flow(form.cleaned_data["start"], form.cleaned_data["end"]) if form.is_valid() else None
    if report and _wants_csv(request):
        r = report
        rows = [["Operating activities", "", ""], ["", "Net income", amt(r.net_income)]]
        if r.noncash:
            rows.append(["", "Depreciation and other non-cash charges", amt(r.noncash)])
        rows += [[l.account.number, l.account.name, amt(l.amount)] for l in r.operating]
        rows.append(["", "Net cash from operating activities", amt(r.total_operating)])
        rows.append(["Investing activities", "", ""])
        rows += [[l.account.number, l.account.name, amt(l.amount)] for l in r.investing]
        rows.append(["", "Net cash from investing activities", amt(r.total_investing)])
        rows.append(["Financing activities", "", ""])
        rows += [[l.account.number, l.account.name, amt(l.amount)] for l in r.financing]
        rows.append(["", "Net cash from financing activities", amt(r.total_financing)])
        rows += [["", "Net change in cash", amt(r.net_change)], ["", "Cash at the start", amt(r.cash_start)],
                 ["", "Cash at the end", amt(r.cash_end)], ["", "Change in bank accounts", amt(r.bank_change)],
                 ["", "Difference", amt(r.difference)]]
        return csv_response(f"cash-flow-{_suffix(r)}", ["Account", "Line", "Amount"], rows)
    return render(request, "ledger/cash_flow.html", {"form": form, "report": report, "nav_active": "reports"})

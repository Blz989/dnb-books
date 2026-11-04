from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from backup.service import backup_status
from core import dashboard
from banking import status as banking_status
from core.roles import is_admin
from handling.periods import due_periods
from items.models import InventorySettings
from items.reports import item_totals, low_stock
from purchasing import reports as purchasing_reports
from purchasing.models import PurchasingSettings
from sales import reports as sales_reports
from sales.models import SalesSettings


@login_required
def home(request):
    stock_value = sum((value for _, value in item_totals().values()), 0)
    today = timezone.localdate()
    overdue = sales_reports.open_invoices(today, overdue_only=True)
    bills = purchasing_reports.bills_due_summary(today)
    handling_due = due_periods(today)
    cash = dashboard.cash_flow(today)
    pnl = dashboard.income_expense(today)
    return render(request, "core/home.html", {
        "cash": cash, "pnl": pnl,
        "cash_legend": [("s1", "Cash in"), ("s2", "Cash out")], "pnl_legend": [("s1", "Income"), ("s2", "Expense")],
        "cash_foot": [("In", f"{cash['total_in']:,.2f}"), ("Out", f"{cash['total_out']:,.2f}"),
                      ("Cash today", f"{cash['ending']:,.2f}")],
        "pnl_foot": [("Income", f"{pnl['total_income']:,.2f}"), ("Expense", f"{pnl['total_expense']:,.2f}"),
                     ("Net", f"{pnl['total_income'] - pnl['total_expense']:,.2f}")],
        "cash_head": ["Month", "Cash in", "Cash out", "Ending cash"], "pnl_head": ["Month", "Income", "Expense", "Net"],
        "cash_rows": [[f"{m.start:%b %Y}", f"{m.a:,.2f}", f"{m.b:,.2f}", f"{m.balance:,.2f}"] for m in cash["months"]],
        "pnl_rows": [[f"{m.start:%b %Y}", f"{m.a:,.2f}", f"{m.b:,.2f}", f"{m.net:,.2f}"] for m in pnl["months"]],
        "open_ar": sales_reports.ar_aging(today).totals.total,
        "overdue_total": sum((r.balance for r in overdue), 0),
        "overdue_count": len(overdue),
        "undeposited": sales_reports.undeposited_funds().total,
        "awaiting_bill": len(sales_reports.drop_ship_awaiting_bill()),
        "sales_unconfigured": is_admin(request.user) and not SalesSettings.load().is_configured,
        "purchasing_unconfigured": is_admin(request.user) and not PurchasingSettings.load().is_configured,
        "bills_due_total": bills.upcoming_total, "bills_due_count": bills.upcoming_count,
        "bills_overdue_total": bills.overdue_total, "bills_overdue_count": bills.overdue_count,
        "bills_due_through": (today + timedelta(days=7)).isoformat(),
        "backup": backup_status(timezone.now()),
        "bank_inbox_count": banking_status.inbox_count(),
        "bank_sync": banking_status.sync_status(),
        "unreconciled": banking_status.unreconciled_accounts(today),
        "handling_due": handling_due[0] if handling_due else None,
        "handling_due_count": len(handling_due),
        "nav_active": "home",
        "stock_value": stock_value,
        "low_stock_count": len(low_stock()),
        "inventory_unconfigured": is_admin(request.user) and not InventorySettings.load().is_configured,
    })


# ---- icons (public: the browser asks for them before anyone signs in) ----

def _icon_response(request, build, content_type: str, *, versioned: bool):
    from django.http import HttpResponse, HttpResponseNotModified

    from . import favicon

    version = favicon.icon_version()
    etag = f'"{version}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(build(), content_type=content_type)
    response["ETag"] = etag
    # A ?v= link changes with the logo, so it can be kept for good; /favicon.ico is rechecked daily.
    long = versioned and request.GET.get("v") == version
    response["Cache-Control"] = "public, max-age=31536000, immutable" if long else "public, max-age=86400"
    return response


def favicon_ico(request):
    from . import favicon

    return _icon_response(request, favicon.render_ico, "image/x-icon", versioned=False)


def brand_icon(request, size: int):
    from django.http import Http404

    from . import favicon

    if size not in favicon.PNG_SIZES:
        raise Http404("No icon of that size.")
    return _icon_response(request, lambda: favicon.render_png(size), "image/png", versioned=True)

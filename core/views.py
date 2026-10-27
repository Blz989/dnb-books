from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from backup.service import backup_status
from core.roles import is_admin
from items.models import InventorySettings
from items.reports import item_totals
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
    return render(request, "core/home.html", {
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
        "nav_active": "home",
        "stock_value": stock_value,
        "inventory_unconfigured": is_admin(request.user) and not InventorySettings.load().is_configured,
    })

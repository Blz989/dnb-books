from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from backup.service import backup_status
from core.roles import is_admin
from items.models import InventorySettings
from items.reports import item_totals


@login_required
def home(request):
    stock_value = sum((value for _, value in item_totals().values()), 0)
    return render(request, "core/home.html", {
        "backup": backup_status(timezone.now()),
        "nav_active": "home",
        "stock_value": stock_value,
        "inventory_unconfigured": is_admin(request.user) and not InventorySettings.load().is_configured,
    })

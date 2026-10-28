from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.roles import require_admin
from ledger.views import _data

from .forms import AddRateForm, HandlingSettingsForm, OrderProfitabilityForm
from .models import HandlingRate, HandlingSettings
from .profit import ZERO, orders
from .rates import add_rate

DEFAULT_PERCENT = Decimal("55")


def _totals(rows) -> dict:
    t = {k: sum((getattr(o, attr) for o in rows), ZERO) for k, attr in [
        ("sales", "net_sales"), ("cost", "cost"), ("profit", "profit"), ("owed", "owed"), ("billed", "billed"),
        ("balance", "balance")]}
    t["margin"] = (t["profit"] / t["sales"] * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if t["sales"] else None
    return t


@login_required
def order_profitability(request):
    today = timezone.localdate()
    form = OrderProfitabilityForm(_data(request, {"from": today.replace(day=1).isoformat(), "to": today.isoformat()}))
    rows = totals = date_from = date_to = None
    if form.is_valid():
        f = form.cleaned_data
        date_from, date_to = f["from"], f["to"]
        # Costs and credits as known now (or as of the end date, if it is later), for orders dated in the range.
        rows = orders(max(today, date_to), date_from=date_from, date_to=date_to, customer=f["customer"])
        if f["unbilled"]:
            rows = [o for o in rows if o.balance != ZERO]
        totals = _totals(rows)
    return render(request, "handling/order_profitability.html", {
        "form": form, "rows": rows, "totals": totals, "date_from": date_from, "date_to": date_to,
        "nav_active": "reports"})


@login_required
def handling_settings(request):
    require_admin(request.user)
    config = HandlingSettings.load()
    form = HandlingSettingsForm(instance=config)
    rate_form = AddRateForm()
    if request.method == "POST" and request.POST.get("action") == "settings":
        form = HandlingSettingsForm(request.POST, instance=config)
        if form.is_valid():
            with transaction.atomic():
                config = form.save()
                if not HandlingRate.objects.exists():
                    add_rate(effective_from=config.start_date, percent=DEFAULT_PERCENT, user=request.user)
            messages.success(request, "Settings saved.")
            return redirect(reverse("handling:settings"))
    elif request.method == "POST" and request.POST.get("action") == "rate":
        rate_form = AddRateForm(request.POST)
        if rate_form.is_valid():
            try:
                add_rate(effective_from=rate_form.cleaned_data["effective_from"],
                         percent=rate_form.cleaned_data["percent"], user=request.user)
            except ValidationError as exc:
                rate_form.add_error(None, exc.messages[0])
            else:
                messages.success(request, "Rate added.")
                return redirect(reverse("handling:settings"))
    return render(request, "handling/settings.html", {
        "form": form, "rate_form": rate_form, "locked": form.locked,
        "rates": HandlingRate.objects.select_related("created_by").order_by("-effective_from"),
        "nav_active": "purchasing-settings"})

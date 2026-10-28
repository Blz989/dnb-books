from decimal import Decimal

from django import template
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from ..models import HandlingRate, HandlingRunLine, HandlingSettings, RunStatus
from ..profit import order_profit
from ..rates import rate_for

register = template.Library()


@register.filter
def percent(rate):
    """A stored fraction (0.5500) as a percent (55%)."""
    if rate is None:
        return "—"
    value = (Decimal(rate) * 100).normalize()
    return f"{value:f}%"


def _run_url(run) -> str | None:
    # The run detail screen comes with the run screens; until that URL exists the label is plain text.
    try:
        return reverse("handling:run", args=[run.pk])
    except NoReverseMatch:
        return None


@register.inclusion_tag("handling/profit_panel.html")
def profit_panel(key):
    """Profit for one order (a sales order number, or a direct invoice number), as of today."""
    p = order_profit(key, timezone.localdate())
    runs = [
        {"label": line.run.label, "url": _run_url(line.run), "amount": line.this_bill}
        for line in HandlingRunLine.objects.filter(order_key=key, run__status=RunStatus.POSTED)
        .select_related("run").order_by("run__period_start")
    ]
    return {"p": p, "runs": runs}


@register.simple_tag
def handling_config():
    """What the Handling section of Purchasing settings shows."""
    return {"settings": HandlingSettings.load(), "current_rate": rate_for(timezone.localdate()), "has_rates": HandlingRate.objects.exists()}

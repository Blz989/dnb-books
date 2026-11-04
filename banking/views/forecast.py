from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from core import charts

from .. import forecast as fc


@login_required
def cash_forecast(request):
    today = timezone.localdate()
    view = request.GET.get("view", "weekly")
    f = fc.build(today, view)
    # Short axis labels: the start date, or the month name in the monthly view.
    labels = [f"{p.start:%b}" if f.view == "monthly" else f"{p.start:%b} {p.start.day}" for p in f.periods]
    tips = [charts.money_tip(p.label, [("In", p.cash_in), ("Out", p.cash_out), ("Cash at end", p.closing)])
            for p in f.periods]
    every = {"daily": 5, "weekly": 2}.get(f.view, 1)
    svg = charts.line(labels, [p.closing for p in f.periods], tips, "Projected cash", every=every)
    rows = [[p.label, f"{p.opening:,.2f}", f"{p.cash_in:,.2f}", f"{p.cash_out:,.2f}", f"{p.closing:,.2f}"]
            for p in f.periods]
    return render(request, "banking/cash_forecast.html", {
        "f": f, "svg": svg, "rows": rows, "views": [(k, v[0]) for k, v in fc.VIEWS.items()],
        "head": ["Period", "Starting", "In", "Out", "Ending"],
        "ins": [x for x in f.flows if x.kind == "in"], "outs": [x for x in f.flows if x.kind == "out"],
        "nav_active": "cash-forecast",
    })

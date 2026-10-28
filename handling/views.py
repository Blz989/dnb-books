from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.roles import is_admin, require_admin
from items.views import _claim_token, _token_used
from ledger.views import _data
from sales.views.common import new_token

from .billing import NOT_SET_UP, HandlingError, create_run, preview, statement_pdf, void_run
from .forms import AddRateForm, HandlingSettingsForm, OrderProfitabilityForm
from .models import HandlingRate, HandlingRun, HandlingSettings, RunStatus, long_date
from .periods import Period, next_to_bill
from .profit import ZERO, orders
from .rates import add_rate

DEFAULT_PERCENT = Decimal("55")
ALREADY_DONE = "That was already done."


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


# ---- runs ----

def _not_ended(period: Period) -> str:
    return f"{period.label} can't be billed until {long_date(period.end)}."


def _latest_posted() -> HandlingRun | None:
    return HandlingRun.objects.filter(status=RunStatus.POSTED).order_by("-period_start").first()


@login_required
def run_list(request):
    today = timezone.localdate()
    upcoming = next_to_bill()
    return render(request, "handling/run_list.html", {
        "runs": HandlingRun.objects.select_related("bill", "vendor_credit"),
        "upcoming": upcoming,
        "can_bill": upcoming is not None and upcoming.end <= today,
        "not_ended": _not_ended(upcoming) if upcoming else "",
        "not_set_up": NOT_SET_UP if upcoming is None else "",
        "nav_active": "handling"})


def _period(period_end) -> Period:
    period = Period.containing(period_end)
    if period.end != period_end:
        raise Http404
    return period


def _problem(period: Period, today) -> tuple[str, str, str]:
    """Why `period` can't be billed now, as (kind, message, link); kind is "" when it can. The same checks as
    create_run, which makes them again when the button is pressed."""
    expected = next_to_bill()
    if expected is None:
        return "not-set-up", NOT_SET_UP, ""
    if period.start < expected.start:
        return "billed", f"{period.label} is already billed.", reverse("handling:runs")
    if period.start > expected.start:
        return "out-of-order", f"Bill {expected.label} first.", reverse("handling:preview", args=[expected.end])
    if period.end > today:
        return "not-ended", _not_ended(period), ""
    return "", "", ""


@login_required
def run_preview(request, period_end):
    period = _period(period_end)
    today = timezone.localdate()
    if request.method == "POST":
        return _create(request, period, today)
    kind, problem, problem_link = _problem(period, today)
    # A billed period's figures would count its own run as billed before, so there is nothing useful to show.
    pv = None if kind in ("not-set-up", "billed") else preview(period)
    if pv is None or pv.total > ZERO:
        button = "Create bill"
    elif pv.total < ZERO:
        button = "Create credit"
    else:
        button = "Record period"
    return render(request, "handling/preview.html", {
        "period": period, "pv": pv, "problem": problem, "problem_link": problem_link,
        "not_set_up": kind == "not-set-up", "can_create": kind == "", "button": button, "token": new_token(),
        "totals": _preview_totals(pv) if pv else None, "nav_active": "handling"})


def _preview_totals(pv) -> dict:
    return {k: sum((getattr(r.order, k) for r in pv.rows), ZERO) for k in ("net_sales", "cost", "profit")} | {
        k: sum((getattr(r, k) for r in pv.rows), ZERO) for k in ("owed_to_date", "billed_before")}


def _create(request, period: Period, today):
    token = request.POST.get("token", "")
    if _token_used(token):
        return _already_done(request, period)
    try:
        with transaction.atomic():
            # The token is claimed inside the same transaction as the run, so a refused run frees it again.
            if not _claim_token(token):
                return _already_done(request, period)
            run = create_run(period, user=request.user, today=today)
    except HandlingError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("handling:runs"))
    doc = run.bill or run.vendor_credit
    messages.success(request, f"Saved {doc.number}." if doc else f"{run.label} is recorded with nothing to bill.")
    return redirect(reverse("handling:run", args=[run.pk]))


def _already_done(request, period: Period):
    messages.info(request, ALREADY_DONE)
    run = HandlingRun.objects.filter(status=RunStatus.POSTED, period_start=period.start).first()
    return redirect(reverse("handling:run", args=[run.pk]) if run else reverse("handling:runs"))


@login_required
def run_detail(request, pk):
    run = get_object_or_404(HandlingRun.objects.select_related("bill", "vendor_credit", "created_by", "voided_by"),
                            pk=pk)
    lines = list(run.lines.select_related("customer", "sales_order").order_by("order_key"))
    latest = _latest_posted()
    return render(request, "handling/run_detail.html", {
        "run": run, "lines": lines, "is_void": run.status == RunStatus.VOID,
        "totals": {k: sum((getattr(l, k) for l in lines), ZERO)
                   for k in ("sales", "cost", "profit", "owed_to_date", "billed_before")},
        "can_void": is_admin(request.user) and latest is not None and latest.pk == run.pk,
        "token": new_token(), "nav_active": "handling"})


@login_required
@require_POST
def run_void(request, pk):
    require_admin(request.user)
    run = get_object_or_404(HandlingRun, pk=pk)
    detail = reverse("handling:run", args=[run.pk])
    token = request.POST.get("token", "")
    if _token_used(token):
        messages.info(request, ALREADY_DONE)
        return redirect(detail)
    try:
        with transaction.atomic():
            if not _claim_token(token):
                messages.info(request, ALREADY_DONE)
                return redirect(detail)
            void_run(run, user=request.user, reason=request.POST.get("reason", ""))
    except HandlingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{run.label} is void.")
    return redirect(detail)


@login_required
def run_statement(request, pk):
    run = get_object_or_404(HandlingRun, pk=pk)
    # A POSTED run serves the statement saved with it. A void run is drawn again from its saved rows, marked VOID.
    if run.status == RunStatus.POSTED and run.statement_pdf:
        with run.statement_pdf.open("rb") as f:
            content = f.read()
    else:
        content = statement_pdf(run)
    response = HttpResponse(content, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="handling-{run.period_end.isoformat()}.pdf"'
    return response

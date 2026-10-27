from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.roles import is_admin, require_admin

from .. import reconcile
from ..forms import ReconcileStartForm
from ..models import BankAccount, Reconciliation, ReconciliationItem, ReconStatus
from ..reconcile import ReconcileError


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


def _rec(account_id: int, pk: int) -> Reconciliation:
    return get_object_or_404(Reconciliation.objects.select_related("bank_account__ledger_account"),
                             pk=pk, bank_account_id=account_id)


def _work_url(rec: Reconciliation) -> str:
    return reverse("banking:reconcile-work", args=[rec.bank_account_id, rec.pk])


@login_required
def reconcile_index(request):
    today = timezone.localdate()
    rows = []
    for account in BankAccount.objects.filter(is_active=True).select_related("ledger_account"):
        last = account.reconciliations.filter(status=ReconStatus.FINISHED).order_by("-statement_date", "-id").first()
        progress = account.reconciliations.filter(status=ReconStatus.IN_PROGRESS).first()
        rows.append({
            "account": account, "last": last, "progress": progress,
            "overdue": last is None or (today - last.statement_date) > timedelta(days=40),
        })
    return render(request, "banking/reconcile_index.html", {"rows": rows, "nav_active": "reconcile"})


@login_required
def reconcile_start(request, account_id):
    account = get_object_or_404(BankAccount.objects.select_related("ledger_account"), pk=account_id)
    in_progress = account.reconciliations.filter(status=ReconStatus.IN_PROGRESS).first()
    if in_progress is not None:
        if request.method == "POST":
            messages.error(request, "Finish or cancel the reconciliation already in progress.")
        return redirect(_work_url(in_progress))
    form = ReconcileStartForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            rec = reconcile.start(account, statement_date=form.cleaned_data["statement_date"],
                                  statement_balance=form.cleaned_data["statement_balance"], user=request.user)
        except ReconcileError as exc:
            messages.error(request, str(exc))
        else:
            return redirect(_work_url(rec))
    history = account.reconciliations.exclude(status=ReconStatus.IN_PROGRESS)[:12]
    last = account.reconciliations.filter(status=ReconStatus.FINISHED).order_by("-statement_date", "-id").first()
    return render(request, "banking/reconcile_start.html", {
        "account": account, "form": form, "history": history, "last": last, "nav_active": "reconcile"})


def _summary_context(rec: Reconciliation) -> dict:
    s = reconcile.summary(rec)
    return {**s, "balanced": s["difference"] == 0}


@login_required
def reconcile_work(request, account_id, pk):
    rec = _rec(account_id, pk)
    rows = reconcile.rows_for(rec)
    deposits = [r for r in rows if r["is_deposit"]]
    payments = [r for r in rows if not r["is_deposit"]]
    context = {"rec": rec, "account": rec.bank_account, "is_liability": rec.bank_account.is_liability,
               "nav_active": "reconcile", **_summary_context(rec)}
    if rec.status == ReconStatus.IN_PROGRESS:
        return render(request, "banking/reconcile_work.html", {
            **context, "deposits": deposits, "payments": payments, "can_finish": is_admin(request.user)})
    cleared = [r for r in rows if r["cleared"]]
    return render(request, "banking/reconcile_done.html", {
        **context, "cleared_deposits": [r for r in cleared if r["is_deposit"]],
        "cleared_payments": [r for r in cleared if not r["is_deposit"]],
        "outstanding": [r for r in rows if not r["cleared"] and not r["is_deposit"]],
        "in_transit": [r for r in rows if not r["cleared"] and r["is_deposit"]],
        "can_undo": is_admin(request.user)})


def _items(rec: Reconciliation):
    return ReconciliationItem.objects.filter(reconciliation=rec)


@login_required
@require_POST
def reconcile_toggle(request, account_id, pk, item_id):
    rec = _rec(account_id, pk)
    item = get_object_or_404(_items(rec), pk=item_id)
    cleared = request.POST.get("cleared") == "1"
    try:
        reconcile.toggle(item, cleared)
    except ReconcileError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    s = _summary_context(rec)
    return JsonResponse({
        "cleared": cleared, "cleared_total": _money(s["cleared_total"]), "cleared_balance": _money(s["cleared_balance"]),
        "difference": _money(s["difference"]), "can_finish": s["balanced"],
    })


@login_required
@require_POST
def reconcile_ticks(request, account_id, pk):
    """The no-JavaScript path: every box on the page is saved at once."""
    rec = _rec(account_id, pk)
    ticked = {int(i) for i in request.POST.getlist("cleared") if i.isdigit()}
    try:
        for item in _items(rec):
            if item.cleared != (item.pk in ticked):
                reconcile.toggle(item, item.pk in ticked)
    except ReconcileError as exc:
        messages.error(request, str(exc))
    return redirect(_work_url(rec))


@login_required
@require_POST
def reconcile_finish(request, account_id, pk):
    require_admin(request.user)
    rec = _rec(account_id, pk)
    try:
        reconcile.finish(rec, user=request.user)
    except ReconcileError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Reconciled {rec.bank_account} to the {rec.statement_date:%B %Y} statement.")
    return redirect(_work_url(rec))


@login_required
@require_POST
def reconcile_cancel(request, account_id, pk):
    rec = _rec(account_id, pk)
    try:
        reconcile.cancel(rec, user=request.user)
    except ReconcileError as exc:
        messages.error(request, str(exc))
        return redirect(_work_url(rec))
    messages.success(request, "Cancelled. Nothing was changed in the books.")
    return redirect(reverse("banking:reconcile-start", args=[account_id]))


@login_required
@require_POST
def reconcile_undo(request, account_id, pk):
    require_admin(request.user)
    rec = _rec(account_id, pk)
    try:
        reconcile.undo(rec, user=request.user, reason=request.POST.get("reason", ""))
    except ReconcileError as exc:
        messages.error(request, str(exc))
        return redirect(_work_url(rec))
    messages.success(request, "Undone. The documents in it can be changed again.")
    return redirect(reverse("banking:reconcile-start", args=[account_id]))


@login_required
def reconcile_report(request, account_id, pk):
    rec = _rec(account_id, pk)
    if rec.status != ReconStatus.FINISHED or not rec.report_pdf:
        raise Http404("No report for this reconciliation.")
    response = FileResponse(rec.report_pdf.open("rb"), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="reconciliation-{rec.statement_date:%Y-%m}.pdf"'
    return response

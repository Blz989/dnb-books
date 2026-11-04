"""One bank account's page: header tiles, then Dashboard, Uncategorized (the inbox), and All transactions tabs.

Read-only. Every action on it is an existing view: inbox actions, Sync now, Import file, Reconcile, new journal
entry, new deposit.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from core import charts
from ledger.balances import money
from ledger.models import JournalLine

from ..models import BankAccount, BankTransaction, Reconciliation, ReconStatus, Rule, TxnSource, TxnStatus
from ..register import build_rows, filtered_lines, ledger_balance, RegisterFilters
from .inbox import inbox_context
from .register import RegisterFilterForm, querystring, register_page

TABS = ("dashboard", "uncategorized", "all")
CHART_DAYS = 30
RECENT = 10
ZERO = Decimal("0.00")


def _bank_tile(account: BankAccount, last_rec) -> dict:
    if account.bank_balance is not None:
        return {"value": account.bank_balance, "note": f"as of {account.bank_balance_on:%Y-%m-%d}"}
    if last_rec is not None and last_rec.statement_balance is not None:
        return {"value": last_rec.statement_balance, "note": f"statement, {last_rec.statement_date:%Y-%m-%d}"}
    return {"value": None, "note": "Not reported"}


def closing_balances(account: BankAccount, today, days: int = CHART_DAYS) -> list[tuple]:
    """(day, ledger balance at the end of that day) for the last `days` days: one opening sum, one grouped sum."""
    first = today - timedelta(days=days - 1)
    lines = JournalLine.objects.in_ledger().filter(account_id=account.ledger_account_id)
    opening = lines.filter(entry__date__lt=first).aggregate(d=Coalesce(Sum("debit"), ZERO), c=Coalesce(Sum("credit"), ZERO))
    per_day = {r["entry__date"]: money(r["d"]) - money(r["c"]) for r in
               lines.filter(entry__date__gte=first, entry__date__lte=today).values("entry__date")
               .annotate(d=Sum("debit"), c=Sum("credit"))}
    sign = -1 if account.is_liability else 1
    running = money(opening["d"]) - money(opening["c"])
    out = []
    for n in range(days):
        day = first + timedelta(days=n)
        running += per_day.get(day, ZERO)
        out.append((day, money(running * sign)))
    return out


def _chart(points) -> str:
    labels = [f"{d:%b} {d.day}" for d, _ in points]
    tips = [charts.money_tip(f"{d:%b} {d.day}, {d.year}", [("Closing balance", v)]) for d, v in points]
    return charts.line(labels, [v for _, v in points], tips, "Closing balance, last 30 days", every=7)


@login_required
def account_detail(request, pk):
    account = get_object_or_404(BankAccount.objects.select_related("ledger_account", "plaid_item"), pk=pk)
    tab = request.GET.get("tab", "dashboard")
    if tab not in TABS:
        tab = "dashboard"
    today = timezone.localdate()
    txns = BankTransaction.objects.filter(bank_account=account)
    counts = txns.aggregate(
        new=Count("pk", filter=Q(status=TxnStatus.NEW)),
        recognized=Count("pk", filter=Q(status__in=[TxnStatus.MATCHED, TxnStatus.BOOKED])),
        last_feed=Max("date"), last_import=Max("created_at", filter=Q(source=TxnSource.FILE)))
    last_rec = (Reconciliation.objects.filter(bank_account=account, status=ReconStatus.FINISHED)
                .order_by("-statement_date", "-id").first())
    books = ledger_balance(account)
    context = {
        "account": account, "tab": tab, "books": books, "bank": _bank_tile(account, last_rec), "counts": counts,
        "item": account.plaid_item, "nav_active": "bank-accounts",
    }

    if tab == "dashboard":
        points = closing_balances(account, today)
        context.update({
            "last_rec": last_rec,
            "rules": Rule.objects.filter(Q(bank_account=account) | Q(bank_account__isnull=True), is_active=True).count(),
            "chart_svg": _chart(points),
            "chart_head": ["Date", "Closing balance"],
            "chart_rows": [(f"{d:%Y-%m-%d}", f"{v:,.2f}") for d, v in points],
            "recent": build_rows(account, list(filtered_lines(account, RegisterFilters())[:RECENT])),
        })
    elif tab == "uncategorized":
        status = TxnStatus.EXCLUDED if request.GET.get("status") == TxnStatus.EXCLUDED else TxnStatus.NEW
        context.update(inbox_context(request, str(account.pk), status))
    else:
        form = RegisterFilterForm(request.GET)
        rows = page = None
        if form.is_valid():
            rows, page = register_page(request, account, form)
        export = querystring(request, drop=("page", "format", "tab"))
        context.update({"form": form, "rows": rows, "page": page, "querystring": querystring(request),
                        "export_query": f"account={account.pk}" + (f"&{export}" if export else "")})
    return render(request, "banking/account_detail.html", context)

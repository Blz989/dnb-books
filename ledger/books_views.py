"""Books: journal entries, chart of accounts, and month close, as app screens (not Django admin)."""
import calendar
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.roles import is_admin, require_admin

from . import sources
from .errors import PeriodCloseBlockedError, PostingError
from .forms import AccountForm, CloseAnywayForm, EntryHeaderForm, ReopenForm, VoidForm
from .locks import void_block_message
from .models import Account, AccountingPeriod, AccountType, EntryStatus, JournalEntry, JournalLine
from .periods import close_period, close_problems, reopen_period
from .posting import LineSpec, post_draft, post_entry

ZERO = Decimal("0")
LINE_ROWS = 20
VISIBLE_ROWS = 4
TYPE_ORDER = [AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY, AccountType.INCOME, AccountType.EXPENSE]
GROUP_LABELS = {AccountType.ASSET: "Assets", AccountType.LIABILITY: "Liabilities", AccountType.EQUITY: "Equity",
                AccountType.INCOME: "Income", AccountType.EXPENSE: "Expenses"}
DEBIT_NORMAL = {AccountType.ASSET, AccountType.EXPENSE}


def _ctx(nav, **extra):
    return {"nav_active": nav, **extra}


# ---- journal entries ----

@login_required
def journal_list(request):
    entries = JournalEntry.objects.annotate(amount=Sum("lines__debit")).order_by("-date", "-id")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    start, end = request.GET.get("start", ""), request.GET.get("end", "")
    if q:
        number = q.upper().removeprefix("JE-")
        match = Q(memo__icontains=q) | Q(reference__icontains=q) | Q(source_id__icontains=q)
        if number.isdigit():
            match |= Q(pk=int(number))
        entries = entries.filter(match)
    if status in EntryStatus.values:
        entries = entries.filter(status=status)
    try:
        if start:
            entries = entries.filter(date__gte=date.fromisoformat(start))
        if end:
            entries = entries.filter(date__lte=date.fromisoformat(end))
    except ValueError:
        messages.error(request, "Enter a date like 2026-01-31.")
    page = Paginator(entries, 50).get_page(request.GET.get("page"))
    rows = [{"entry": e, "source": sources.describe(e)} for e in page]
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "books/journal_list.html", _ctx(
        "journal", page=page, rows=rows, q=q, status=status, start=start, end=end,
        statuses=EntryStatus.choices, query=query.urlencode(),
    ))


def _entry_lines(entry):
    lines = list(entry.lines.select_related("account"))
    debit = sum((l.debit for l in lines), ZERO)
    credit = sum((l.credit for l in lines), ZERO)
    return lines, debit, credit


@login_required
def journal_detail(request, pk):
    entry = get_object_or_404(JournalEntry.objects.select_related("created_by", "posted_by", "reverses"), pk=pk)
    lines, debit, credit = _entry_lines(entry)
    can_void = entry.status == EntryStatus.POSTED and is_admin(request.user)
    return render(request, "books/journal_detail.html", _ctx(
        "journal", entry=entry, lines=lines, total_debit=debit, total_credit=credit,
        source=sources.describe(entry), reversals=list(entry.reversals.all()),
        can_void=can_void, void_blocked=void_block_message(entry) if can_void else None,
        is_draft=entry.status == EntryStatus.DRAFT,
    ))


def _line_rows(post=None):
    rows = []
    for n in range(1, LINE_ROWS + 1):
        get = (lambda f: post.get(f"{f}_{n}", "").strip()) if post else (lambda f: "")
        rows.append({"n": n, "account": get("account"), "description": get("description"),
                     "debit": get("debit"), "credit": get("credit"), "hidden": n > VISIBLE_ROWS})
    if post:
        last_used = max([r["n"] for r in rows if any(r[k] for k in ("account", "description", "debit", "credit"))] or [0])
        for r in rows:
            r["hidden"] = r["n"] > max(VISIBLE_ROWS, last_used)
    return rows


def _amount(raw: str, n: int) -> Decimal:
    if not raw:
        return ZERO
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise PostingError(f"Line {n}: enter an amount like 125.00.")
    if not value.is_finite():
        raise PostingError(f"Line {n}: enter an amount like 125.00.")
    return value


def _parse_lines(rows) -> list[LineSpec]:
    accounts = {str(a.pk): a for a in Account.objects.filter(pk__in=[r["account"] for r in rows if r["account"].isdigit()])}
    specs = []
    for r in rows:
        if not any(r[k] for k in ("account", "description", "debit", "credit")):
            continue
        n = len(specs) + 1
        account = accounts.get(r["account"])
        if account is None:
            raise PostingError(f"Line {n}: choose an account.")
        specs.append(LineSpec(account, _amount(r["debit"], n), _amount(r["credit"], n), r["description"][:200]))
    return specs


@login_required
def journal_new(request):
    if not request.user.has_perm("ledger.add_journalentry") and not is_admin(request.user):
        raise Http404
    form = EntryHeaderForm(request.POST or None, initial={"date": timezone.localdate()})
    rows = _line_rows(request.POST if request.method == "POST" else None)
    error = None
    if request.method == "POST" and form.is_valid():
        try:
            entry = post_entry(
                date=form.cleaned_data["date"], lines=_parse_lines(rows), user=request.user,
                memo=form.cleaned_data["memo"], reference=form.cleaned_data["reference"],
            )
        except PostingError as exc:
            error = str(exc)
        else:
            messages.success(request, f"{entry.display_number} posted.")
            return redirect(reverse("books:journal-detail", args=[entry.pk]))
    accounts = Account.objects.filter(is_active=True).order_by("number")
    return render(request, "books/journal_form.html", _ctx(
        "journal", form=form, rows=rows, accounts=accounts, error=error, line_rows=LINE_ROWS,
    ))


@login_required
def journal_void(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk)
    require_admin(request.user)
    from .posting import void_entry

    form = VoidForm(request.POST or None, initial={"date": entry.date})
    if request.method == "POST" and form.is_valid():
        try:
            reversal = void_entry(entry, user=request.user, reason=form.cleaned_data["reason"],
                                  date=form.cleaned_data["date"])
        except PostingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"{entry.display_number} was voided by {reversal.display_number}.")
            return redirect(reverse("books:journal-detail", args=[entry.pk]))
    return render(request, "books/journal_void.html", _ctx("journal", entry=entry, form=form))


@login_required
@require_POST
def journal_post_draft(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk)
    if not request.user.has_perm("ledger.change_journalentry") and not is_admin(request.user):
        raise Http404
    try:
        post_draft(entry, request.user)
    except PostingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{entry.display_number} posted.")
    return redirect(reverse("books:journal-detail", args=[entry.pk]))


@login_required
@require_POST
def journal_delete_draft(request, pk):
    entry = get_object_or_404(JournalEntry, pk=pk)
    if not request.user.has_perm("ledger.delete_journalentry") and not is_admin(request.user):
        raise Http404
    if entry.status != EntryStatus.DRAFT:
        messages.error(request, "Only draft entries can be deleted.")
        return redirect(reverse("books:journal-detail", args=[entry.pk]))
    number = entry.display_number
    with transaction.atomic():
        entry.lines.all().delete()
        entry.delete()
    messages.success(request, f"Draft {number} deleted.")
    return redirect(reverse("books:journal"))


# ---- chart of accounts ----

def _balances() -> dict[int, Decimal]:
    rows = JournalLine.objects.in_ledger().values("account").annotate(d=Sum("debit"), c=Sum("credit"))
    return {r["account"]: (r["d"] or ZERO) - (r["c"] or ZERO) for r in rows}


@login_required
def accounts(request):
    show_inactive = request.GET.get("inactive") == "1"
    qs = Account.objects.order_by("number")
    if not show_inactive:
        qs = qs.filter(is_active=True)
    balances = _balances()
    groups = []
    for account_type in TYPE_ORDER:
        rows = []
        for a in qs:
            if a.type != account_type:
                continue
            net = balances.get(a.pk, ZERO)
            rows.append({"account": a, "balance": net if account_type in DEBIT_NORMAL else -net})
        if rows:
            groups.append({"label": GROUP_LABELS[account_type], "rows": rows})
    from banking.models import BankAccount

    bank_pages = dict(BankAccount.objects.values_list("ledger_account_id", "pk"))
    for g in groups:
        for r in g["rows"]:
            r["bank_page"] = bank_pages.get(r["account"].pk)
    return render(request, "books/accounts.html", _ctx(
        "accounts", groups=groups, show_inactive=show_inactive,
        can_edit=request.user.has_perm("ledger.change_account") or is_admin(request.user),
    ))


def _account_form(request, account):
    if not (request.user.has_perm("ledger.add_account") or is_admin(request.user)):
        raise Http404
    has_lines = bool(account and account.lines.exists())
    form = AccountForm(request.POST or None, instance=account, has_lines=has_lines)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"Account {saved.number} {saved.name} saved.")
        return redirect(reverse("books:accounts"))
    return render(request, "books/account_form.html", _ctx("accounts", form=form, account=account))


@login_required
def account_new(request):
    return _account_form(request, None)


@login_required
def account_edit(request, pk):
    return _account_form(request, get_object_or_404(Account, pk=pk))


# ---- month close ----

def _months():
    """Every month from the first entry (or first period) through this month, newest first."""
    today = timezone.localdate()
    first = JournalEntry.objects.order_by("date").values_list("date", flat=True).first() or today
    first_period = AccountingPeriod.objects.order_by("year", "month").first()
    y, m = first.year, first.month
    if first_period and (first_period.year, first_period.month) < (y, m):
        y, m = first_period.year, first_period.month
    out = []
    while (y, m) <= (today.year, today.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return list(reversed(out))


def _parse_month(month: str) -> tuple[int, int]:
    try:
        y, m = month.split("-")
        y, m = int(y), int(m)
        if not 1 <= m <= 12:
            raise ValueError
    except ValueError:
        raise Http404
    return y, m


@login_required
def periods(request):
    stored = {(p.year, p.month): p for p in AccountingPeriod.objects.select_related("closed_by")}
    rows = []
    for y, m in _months():
        p = stored.get((y, m))
        rows.append({"key": f"{y}-{m:02d}", "label": f"{calendar.month_name[m]} {y}", "period": p,
                     "closed": bool(p and p.is_closed)})
    return render(request, "books/periods.html", _ctx("periods", rows=rows, can_manage=is_admin(request.user)))


@login_required
@require_POST
def period_close(request, month):
    require_admin(request.user)
    y, m = _parse_month(month)
    try:
        close_period(y, m, request.user)
    except PeriodCloseBlockedError:
        return redirect(reverse("books:period-close-anyway", args=[month]))
    except PostingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{calendar.month_name[m]} {y} is closed.")
    return redirect(reverse("books:periods"))


@login_required
def period_close_anyway(request, month):
    require_admin(request.user)
    y, m = _parse_month(month)
    problems = close_problems(y, m)
    form = CloseAnywayForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        close_period(y, m, request.user, form.cleaned_data["reason"])
        messages.success(request, f"{calendar.month_name[m]} {y} is closed.")
        return redirect(reverse("books:periods"))
    return render(request, "books/period_close_anyway.html", _ctx(
        "periods", label=f"{calendar.month_name[m]} {y}", problems=problems, form=form))


@login_required
def period_reopen(request, month):
    require_admin(request.user)
    y, m = _parse_month(month)
    form = ReopenForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reopen_period(y, m, request.user, form.cleaned_data["reason"])
        messages.success(request, f"{calendar.month_name[m]} {y} was reopened.")
        return redirect(reverse("books:periods"))
    return render(request, "books/period_reopen.html", _ctx(
        "periods", label=f"{calendar.month_name[m]} {y}", form=form))

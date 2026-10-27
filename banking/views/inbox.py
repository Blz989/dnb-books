from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ledger.models import Account

from .. import inbox
from ..forms import RuleForm
from ..inbox import InboxError, candidate_key, candidates, match_label
from ..models import BankAccount, BankTransaction, Rule, TxnStatus
from . import common

PAGE_SIZE = 100
STATUS_CHOICES = [(TxnStatus.NEW, "New"), (TxnStatus.MATCHED, "Matched"), (TxnStatus.BOOKED, "Booked"),
                  (TxnStatus.EXCLUDED, "Excluded"), ("ALL", "All")]


def _inbox_url(request) -> str:
    return request.POST.get("next") or reverse("banking:inbox")


@login_required
def inbox_list(request):
    status = request.GET.get("status", TxnStatus.NEW)
    if status not in dict(STATUS_CHOICES):
        status = TxnStatus.NEW
    account_id = request.GET.get("account", "")
    rows = BankTransaction.objects.select_related("bank_account__ledger_account", "match")
    if status != "ALL":
        rows = rows.filter(status=status)
    if account_id.isdigit():
        rows = rows.filter(bank_account_id=int(account_id))
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    items = []
    for txn in page:
        item = {"txn": txn, "token": common.new_token(), "suggestion": None, "matched": ""}
        if txn.status == TxnStatus.NEW:
            item["suggestion"] = inbox.suggest(txn)
        elif txn.status in (TxnStatus.MATCHED, TxnStatus.BOOKED) and hasattr(txn, "match"):
            item["matched"] = match_label(txn.match)
        items.append(item)
    return render(request, "banking/inbox.html", {
        "items": items, "page": page, "status": status, "statuses": STATUS_CHOICES, "account_id": account_id,
        "accounts": BankAccount.objects.select_related("ledger_account"), "bulk_token": common.new_token(),
        "nav_active": "inbox"})


def _result_message(request, txn, match) -> None:
    if txn.status == TxnStatus.BOOKED:
        messages.success(request, f"Booked {abs(txn.amount):,.2f} to {_other_account(match, txn)}.")
    else:
        messages.success(request, f"Matched to {match_label(match)}.")


def _other_account(match, txn) -> str:
    line = match.journal_entry.lines.exclude(account=txn.bank_account.ledger_account).select_related("account").first()
    return str(line.account) if line else "the books"


def _run(request, txn, action, error_context=None):
    """Claim the one-time token and run `action` in one transaction. Returns a response, or None on success."""
    token = request.POST.get("token", "")
    try:
        with transaction.atomic():
            if not common._claim_token(token):
                messages.info(request, common.ALREADY_DONE)
                return redirect(_inbox_url(request))
            match = action()
    except InboxError as exc:
        if error_context is not None:
            return render(request, "banking/inbox_change.html", {**error_context, "errors": [str(exc)]})
        messages.error(request, str(exc))
        return redirect(_inbox_url(request))
    txn.refresh_from_db()
    _result_message(request, txn, match)
    return None


@login_required
@require_POST
def confirm_row(request, pk):
    txn = get_object_or_404(BankTransaction, pk=pk)
    if common._token_used(request.POST.get("token", "")):
        messages.info(request, common.ALREADY_DONE)
        return redirect(_inbox_url(request))
    suggestion = inbox.suggest(txn) if txn.status == TxnStatus.NEW else None
    if suggestion is None:
        messages.error(request, "There is no suggestion for this row. Use Change.")
        return redirect(_inbox_url(request))
    return _run(request, txn, lambda: inbox.confirm(txn, suggestion, user=request.user)) or redirect(_inbox_url(request))


@login_required
def change_row(request, pk):
    txn = get_object_or_404(BankTransaction.objects.select_related("bank_account__ledger_account"), pk=pk)
    found = candidates(txn)
    context = {
        "txn": txn, "candidates": [{"c": c, "key": candidate_key(c)} for c in found], "token": common.new_token(),
        "accounts": Account.objects.filter(is_active=True).exclude(pk=txn.bank_account.ledger_account_id).order_by("number"),
        "nav_active": "inbox",
    }
    if request.method != "POST":
        return render(request, "banking/inbox_change.html", context)
    context["token"] = request.POST.get("token", "") or context["token"]
    if txn.status != TxnStatus.NEW:
        messages.info(request, common.ALREADY_DONE)
        return redirect(reverse("banking:inbox"))
    if request.POST.get("action") == "candidate":
        chosen = next((c for c in found if candidate_key(c) == request.POST.get("candidate", "")), None)
        if chosen is None:
            return render(request, "banking/inbox_change.html", {
                **context, "errors": ["That suggestion is no longer available."]})
        response = _run(request, txn, lambda: inbox.confirm(txn, chosen, user=request.user), context)
    else:
        account = Account.objects.filter(pk=request.POST["account"], is_active=True).first() \
            if request.POST.get("account", "").isdigit() else None
        if account is None:
            return render(request, "banking/inbox_change.html", {
                **context, "errors": ["Choose the account to book it to."]})
        memo = request.POST.get("memo", "").strip()
        response = _run(request, txn, lambda: inbox.new_entry(txn, account=account, user=request.user, memo=memo),
                        context)
    return response or redirect(reverse("banking:inbox"))


@login_required
@require_POST
def exclude_row(request, pk):
    txn = get_object_or_404(BankTransaction, pk=pk)
    reason = request.POST.get("reason", "")
    if not reason.strip():
        messages.error(request, "Enter a reason.")
        return redirect(_inbox_url(request))
    try:
        with transaction.atomic():
            if not common._claim_token(request.POST.get("token", "")):
                messages.info(request, common.ALREADY_DONE)
                return redirect(_inbox_url(request))
            inbox.exclude(txn, reason=reason, user=request.user)
    except InboxError as exc:
        messages.error(request, str(exc))
        return redirect(_inbox_url(request))
    messages.success(request, "Excluded. You can restore it from the Excluded filter.")
    return redirect(_inbox_url(request))


@login_required
@require_POST
def restore_row(request, pk):
    txn = get_object_or_404(BankTransaction, pk=pk)
    try:
        inbox.restore(txn, user=request.user)
    except InboxError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Restored to the inbox.")
    return redirect(_inbox_url(request))


@login_required
@require_POST
def bulk_confirm(request):
    ids = [int(i) for i in request.POST.getlist("rows") if i.isdigit()]
    if not ids:
        messages.error(request, "Tick at least one row.")
        return redirect(_inbox_url(request))
    txns = list(BankTransaction.objects.filter(pk__in=ids).select_related("bank_account__ledger_account"))
    with transaction.atomic():
        if not common._claim_token(request.POST.get("token", "")):
            messages.info(request, common.ALREADY_DONE)
            return redirect(_inbox_url(request))
        done = inbox.bulk_confirm(txns, user=request.user)
    messages.success(request, f"Confirmed {done} of {len(txns)} selected.")
    return redirect(_inbox_url(request))


# ---- rules ----

@login_required
def rule_list(request):
    return render(request, "banking/rules.html", {
        "rules": Rule.objects.select_related("account", "bank_account__ledger_account"), "nav_active": "inbox"})


def _rule_form(request, instance=None):
    form = RuleForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        rule = form.save(commit=False)
        if instance is None:
            last = Rule.objects.order_by("-position").first()
            rule.position = (last.position + 1) if last else 1
        rule.save()
        messages.success(request, "Saved the rule.")
        return redirect(reverse("banking:rules"))
    return render(request, "banking/rule_form.html", {"form": form, "rule": instance, "nav_active": "inbox"})


@login_required
def rule_new(request):
    return _rule_form(request)


@login_required
def rule_edit(request, pk):
    return _rule_form(request, get_object_or_404(Rule, pk=pk))


@login_required
@require_POST
def rule_toggle(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    rule.is_active = not rule.is_active
    rule.save()
    return redirect(reverse("banking:rules"))


@login_required
@require_POST
def rule_move(request, pk):
    rule = get_object_or_404(Rule, pk=pk)
    ordered = list(Rule.objects.order_by("position", "id"))
    i = next(n for n, r in enumerate(ordered) if r.pk == rule.pk)
    j = i - 1 if request.POST.get("direction") == "up" else i + 1
    if 0 <= j < len(ordered):
        ordered[i], ordered[j] = ordered[j], ordered[i]
    for position, r in enumerate(ordered, start=1):
        if r.position != position:
            r.position = position
            r.save()
    return redirect(reverse("banking:rules"))

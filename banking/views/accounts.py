import json
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.roles import require_admin
from ledger.balances import account_totals
from ledger.models import Account

from .. import plaid_client
from ..crypto import FieldKeyError
from ..forms import (
    BankAccountForm, CsvMappingForm, ImportUploadForm, csv_headers, guess_mapping, ledger_choices,
)
from ..importers import CsvImportError, CsvMapping, import_csv, import_transactions, parse_csv, parse_ofx
from ..models import (
    AccountKind, BankAccount, ItemStatus, PlaidItem, Reconciliation, ReconStatus, TxnSource, TxnStatus,
)
from ..plaid_client import PlaidError, plaid_configured
from ..sync import sync_item

NOT_CONFIGURED = "Plaid isn't set up. Add PLAID_CLIENT_ID and PLAID_SECRET to .env."
PREVIEW_ROWS = 5


# ---- account list and form ----

@login_required
def account_list(request):
    totals = account_totals(start=None, end=date.max)
    last_done = {
        r.bank_account_id: r.statement_date
        for r in Reconciliation.objects.filter(status=ReconStatus.FINISHED).order_by("statement_date")
    }
    accounts = BankAccount.objects.select_related("ledger_account", "plaid_item").annotate(
        inbox=Count("transactions", filter=Q(transactions__status=TxnStatus.NEW)))
    rows = []
    for a in accounts:
        debit, credit = totals.get(a.ledger_account_id, (Decimal("0.00"), Decimal("0.00")))
        rows.append({
            "account": a, "balance": credit - debit if a.is_liability else debit - credit,
            "last_reconciled": last_done.get(a.pk), "item": a.plaid_item,
        })
    return render(request, "banking/accounts.html", {
        "rows": rows, "plaid_ready": plaid_configured(), "nav_active": "bank-accounts",
    })


@login_required
def account_new(request):
    require_admin(request.user)
    form = BankAccountForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        account = form.save()
        messages.success(request, f"Added {account}.")
        return redirect(reverse("banking:accounts"))
    return render(request, "banking/account_form.html", {"form": form, "nav_active": "bank-accounts", "editing": False})


@login_required
def account_edit(request, pk):
    require_admin(request.user)
    account = get_object_or_404(BankAccount, pk=pk)
    form = BankAccountForm(request.POST or None, instance=account)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved {account}.")
        return redirect(reverse("banking:accounts"))
    return render(request, "banking/account_form.html", {
        "form": form, "account": account, "nav_active": "bank-accounts", "editing": True})


# ---- Plaid ----

def _count_text(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _report_sync(request, item: PlaidItem, result) -> None:
    item.refresh_from_db()
    if item.status != ItemStatus.OK:
        messages.error(request, f"{item.institution_name}: {item.last_error}")
    else:
        messages.success(request, f"{item.institution_name}: {_count_text(result.added, 'new transaction')}.")


@login_required
def link_page(request):
    require_admin(request.user)
    return render(request, "banking/link.html", {
        "configured": plaid_configured(), "not_configured": NOT_CONFIGURED, "nav_active": "bank-accounts",
        "mode": "new", "token_url": reverse("banking:link-token"), "exchange_url": reverse("banking:link-exchange"),
    })


@login_required
def link_update_page(request, pk):
    require_admin(request.user)
    item = get_object_or_404(PlaidItem, pk=pk)
    return render(request, "banking/link.html", {
        "configured": plaid_configured(), "not_configured": NOT_CONFIGURED, "nav_active": "bank-accounts",
        "mode": "update", "item": item,
        "token_url": reverse("banking:link-update-token", args=[item.pk]),
        "done_url": reverse("banking:link-relinked", args=[item.pk]),
    })


def _json_error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


@login_required
@require_POST
def link_token(request):
    require_admin(request.user)
    if not plaid_configured():
        return _json_error(NOT_CONFIGURED)
    try:
        return JsonResponse({"link_token": plaid_client.get_client().create_link_token(str(request.user.pk))})
    except PlaidError as exc:
        return _json_error(exc.message)


@login_required
@require_POST
def link_update_token(request, pk):
    require_admin(request.user)
    item = get_object_or_404(PlaidItem, pk=pk)
    if not plaid_configured():
        return _json_error(NOT_CONFIGURED)
    try:
        client = plaid_client.get_client()
        return JsonResponse({"link_token": client.create_link_token(str(request.user.pk), item.get_access_token())})
    except (PlaidError, FieldKeyError) as exc:
        return _json_error(getattr(exc, "message", None) or str(exc))


@login_required
@require_POST
def link_exchange(request):
    require_admin(request.user)
    if not plaid_configured():
        return _json_error(NOT_CONFIGURED)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return _json_error("That request couldn't be read.")
    public_token = str(body.get("public_token") or "")
    if not public_token:
        return _json_error("Plaid didn't send a token. Try linking again.")
    try:
        access_token, item_id = plaid_client.get_client().exchange_public_token(public_token)
        item = PlaidItem(institution_name=str(body.get("institution_name") or "Bank")[:200], plaid_item_id=item_id)
        item.set_access_token(access_token)
    except PlaidError as exc:
        return _json_error(exc.message)
    except FieldKeyError as exc:
        return _json_error(str(exc))
    item.save()
    return JsonResponse({"redirect": reverse("banking:link-map", args=[item.pk])})


@login_required
@require_POST
def link_relinked(request, pk):
    require_admin(request.user)
    item = get_object_or_404(PlaidItem, pk=pk)
    item.status, item.last_error = ItemStatus.OK, ""
    item.save()
    result = sync_item(item)
    _report_sync(request, item, result)
    return JsonResponse({"redirect": reverse("banking:accounts")})


def _kind_guess(plaid_account) -> str:
    if plaid_account.type == "credit":
        return AccountKind.CARD
    if plaid_account.type == "loan":
        return AccountKind.LOAN
    return AccountKind.SAVINGS if "saving" in plaid_account.subtype else AccountKind.CHECKING


@login_required
def link_map(request, pk):
    require_admin(request.user)
    item = get_object_or_404(PlaidItem, pk=pk)
    try:
        plaid_accounts = plaid_client.get_client().get_accounts(item.get_access_token())
    except (PlaidError, FieldKeyError) as exc:
        return render(request, "banking/link_map.html", {
            "item": item, "rows": [], "errors": [getattr(exc, "message", None) or str(exc)], "nav_active": "bank-accounts"})
    existing = {a.plaid_account_id: a for a in item.accounts.all()}
    rows = []
    for pa in plaid_accounts:
        chosen = request.POST.get(f"ledger-{pa.account_id}") if request.method == "POST" else (
            str(existing[pa.account_id].ledger_account_id) if pa.account_id in existing else "")
        kind = request.POST.get(f"kind-{pa.account_id}") if request.method == "POST" else (
            existing[pa.account_id].kind if pa.account_id in existing else _kind_guess(pa))
        rows.append({"plaid": pa, "ledger": chosen or "", "kind": kind})
    context = {
        "item": item, "rows": rows, "kinds": AccountKind.choices, "ledger_accounts": ledger_choices(exclude_used=False),
        "nav_active": "bank-accounts",
    }
    if request.method != "POST":
        return render(request, "banking/link_map.html", context)

    errors = []
    try:
        with transaction.atomic():
            made = 0
            for row in rows:
                if not row["ledger"].isdigit():
                    continue
                ledger = Account.objects.filter(pk=int(row["ledger"])).first()
                if ledger is None:
                    errors.append(f"{row['plaid'].name}: choose a ledger account from the list.")
                    continue
                account = BankAccount.objects.filter(ledger_account=ledger).first() or BankAccount(ledger_account=ledger)
                account.kind = row["kind"]
                account.plaid_item, account.plaid_account_id, account.mask = item, row["plaid"].account_id, row["plaid"].mask
                try:
                    account.full_clean()
                except Exception as exc:  # ValidationError
                    problems = getattr(exc, "messages", [str(exc)])
                    errors.append(f"{row['plaid'].name}: {' '.join(problems)}")
                    continue
                account.save()
                made += 1
            if errors:
                raise ValueError("rollback")
    except ValueError:
        return render(request, "banking/link_map.html", {**context, "errors": errors})
    result = sync_item(item)
    messages.success(request, f"Linked {_count_text(made, 'account')} at {item.institution_name}.")
    _report_sync(request, item, result)
    return redirect(reverse("banking:accounts"))


@login_required
@require_POST
def sync_now(request, pk):
    require_admin(request.user)
    item = get_object_or_404(PlaidItem, pk=pk)
    result = sync_item(item)
    _report_sync(request, item, result)
    return redirect(reverse("banking:accounts"))


# ---- file import ----

def _session_key(pk: int) -> str:
    return f"bank-import-{pk}"


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _result_redirect(account: BankAccount, result):
    return redirect(
        f"{reverse('banking:import-result', args=[account.pk])}"
        f"?added={result.added}&skipped={result.skipped}&dups={result.possible_duplicates}")


@login_required
def import_file(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    form = ImportUploadForm(request.POST or None, request.FILES or None)
    context = {"account": account, "form": form, "nav_active": "bank-accounts"}
    if request.method != "POST" or not form.is_valid():
        return render(request, "banking/import.html", context)
    upload = form.cleaned_data["file"]
    data = upload.read()
    name = upload.name.lower()
    if name.endswith((".ofx", ".qfx")):
        try:
            txns = parse_ofx(data)
        except ValueError as exc:
            return render(request, "banking/import.html", {**context, "errors": [str(exc)]})
        with transaction.atomic():
            result = import_transactions(account, txns, source=TxnSource.FILE, user=request.user)
        return _result_redirect(account, result)

    text = _decode(data)
    if form.cleaned_data["review_mapping"] or not account.csv_mapping:
        request.session[_session_key(account.pk)] = text
        return redirect(reverse("banking:import-map", args=[account.pk]))
    try:
        with transaction.atomic():
            result = import_csv(account, text, user=request.user)
    except CsvImportError as exc:
        request.session[_session_key(account.pk)] = text
        return render(request, "banking/import.html", {
            **context, "errors": [str(e) for e in exc.errors],
            "hint": "Nothing was saved. Fix the file, or review the column mapping."})
    except TypeError:
        request.session[_session_key(account.pk)] = text
        return redirect(reverse("banking:import-map", args=[account.pk]))
    return _result_redirect(account, result)


def _preview_text(text: str) -> str:
    lines = [ln for ln in text.lstrip("﻿").splitlines() if ln.strip()]
    return "\n".join(lines[:PREVIEW_ROWS + 1])


@login_required
def import_map(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    text = request.session.get(_session_key(account.pk))
    if not text:
        return redirect(reverse("banking:import-file", args=[account.pk]))
    headers = csv_headers(text)
    if request.method == "POST":
        form = CsvMappingForm(request.POST, headers=headers)
    else:
        initial = {**guess_mapping(headers), **{k: v for k, v in account.csv_mapping.items() if v}}
        form = CsvMappingForm(initial=initial, headers=headers)
    context = {"account": account, "form": form, "headers": headers, "nav_active": "bank-accounts", "preview": []}
    if request.method == "POST" and form.is_valid():
        mapping = CsvMapping(**{k: (v or None) if k not in ("date", "description", "date_format") else v
                                for k, v in form.cleaned_data.items()})
        preview, preview_errors = parse_csv(_preview_text(text), mapping)
        context.update(preview=preview, errors=[str(e) for e in preview_errors])
        if request.POST.get("action") == "import" and not preview_errors:
            txns, errors = parse_csv(text, mapping)
            if errors:
                context["errors"] = [str(e) for e in errors]
                context["hint"] = "Nothing was saved."
            else:
                with transaction.atomic():
                    account.csv_mapping = asdict(mapping)
                    account.save()
                    result = import_transactions(account, txns, source=TxnSource.FILE, user=request.user)
                request.session.pop(_session_key(account.pk), None)
                return _result_redirect(account, result)
    elif request.method != "POST":
        initial = form.initial
        try:
            mapping = CsvMapping(
                date=initial["date"], description=initial["description"], amount=initial.get("amount") or None,
                debit=initial.get("debit") or None, credit=initial.get("credit") or None,
                date_format=initial.get("date_format") or "%m/%d/%Y", check_number=initial.get("check_number") or None)
            if initial["date"] and initial["description"]:
                preview, preview_errors = parse_csv(_preview_text(text), mapping)
                context.update(preview=preview, errors=[str(e) for e in preview_errors])
        except KeyError:
            pass
    return render(request, "banking/import_map.html", context)


@login_required
def import_result(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)

    def number(name):
        value = request.GET.get(name, "0")
        return int(value) if value.isdigit() and len(value) < 7 else 0

    dups = number("dups")
    return render(request, "banking/import_result.html", {
        "account": account, "added": number("added"), "skipped": number("skipped"), "dups": dups,
        "dup_word": "possible duplicate" if dups == 1 else "possible duplicates", "nav_active": "bank-accounts"})

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.roles import require_admin
from ledger.locks import lock_message
from purchasing.models import VendorPayment
from sales.models import DocStatus, Refund

from .. import checknumbers
from ..checknumbers import is_to_print
from ..checks import (
    DEFAULT_LAYOUT, FIELD_LABELS, LayoutError, calibration_pdf, check_context, check_pdf, layout_for, save_layout,
)
from ..models import BankAccount, CheckNumber, CheckStatus
from . import common


class PrintError(Exception):
    """A problem a person can fix. Messages are plain language."""


def _find(number: str):
    doc = VendorPayment.objects.select_related("vendor", "paid_from").filter(number=number).first()
    if doc is None:
        doc = Refund.objects.select_related("customer", "bank_account").filter(number=number).first()
    if doc is None:
        raise Http404("No such check.")
    return doc


def _is_refund(doc) -> bool:
    return isinstance(doc, Refund)


def _ledger(doc):
    return doc.bank_account if _is_refund(doc) else doc.paid_from


def _number_of(doc) -> str:
    return (doc.reference if _is_refund(doc) else doc.check_number).strip()


def _party(doc) -> str:
    return doc.customer.name if _is_refund(doc) else doc.vendor.name


def _detail_url(doc) -> str:
    if _is_refund(doc):
        return reverse("sales:refund-detail", args=[doc.number])
    return reverse("purchasing:payment-detail", args=[doc.number])


def _print_url(doc, number: str = "") -> str:
    url = reverse("banking:check-print", args=[doc.number])
    return f"{url}?number={number}" if number else url


def _bank_account(doc) -> BankAccount | None:
    return BankAccount.objects.filter(ledger_account=_ledger(doc)).first()


def _set_number(doc, number: str) -> None:
    if _is_refund(doc):
        doc.reference = number
    else:
        doc.check_number = number
    doc.save()


@login_required
def checks_to_print(request):
    rows = [
        {"doc": p, "kind": "Vendor payment", "party": p.vendor.name, "account": p.paid_from, "url": _print_url(p)}
        for p in VendorPayment.objects.filter(status=DocStatus.OPEN, method="CHECK", check_number="")
        .select_related("vendor", "paid_from")
    ] + [
        {"doc": r, "kind": "Refund", "party": r.customer.name, "account": r.bank_account, "url": _print_url(r)}
        for r in Refund.objects.filter(status=DocStatus.OPEN, method="CHECK", reference="")
        .select_related("customer", "bank_account")
    ]
    rows.sort(key=lambda r: (r["doc"].date, r["doc"].number))
    return render(request, "banking/checks_to_print.html", {"rows": rows, "nav_active": "checks"})


def _already_printed(request, doc):
    number = _number_of(doc)
    if doc.status == DocStatus.VOID:
        messages.error(request, f"{doc.number} is void.")
    elif number:
        messages.info(request, f"{doc.number} was already printed as check {number}.")
    else:
        messages.info(request, f"{doc.number} is not a check.")
    return redirect(_detail_url(doc))


@login_required
def check_print(request, number):
    doc = _find(number)
    if not is_to_print(doc):
        return _already_printed(request, doc)
    bank = _bank_account(doc)
    if bank is None:
        return render(request, "banking/print_check.html", {
            "doc": doc, "party": _party(doc), "errors": [
                f"{_ledger(doc)} isn't set up as a bank account yet. Add it under Banking, Bank accounts."],
            "nav_active": "checks"})
    chosen = request.GET.get("number", "").strip()
    number_value = chosen or str(checknumbers.next_number(bank))
    errors = []
    try:
        checknumbers.assert_free(_ledger(doc), number_value, error=PrintError)
    except PrintError as exc:
        errors.append(str(exc))
    return render(request, "banking/print_check.html", {
        "doc": doc, "party": _party(doc), "bank": bank, "number": number_value, "errors": errors,
        "ready": not errors, "token": common.new_token(), "is_refund": _is_refund(doc),
        "pdf_url": reverse("banking:check-pdf", args=[doc.number]) + f"?number={number_value}",
        "nav_active": "checks"})


@login_required
def check_pdf_view(request, number):
    doc = _find(number)
    shown = request.GET.get("number", "").strip() or _number_of(doc)
    if not shown:
        raise Http404("Choose a check number first.")
    response = HttpResponse(check_pdf(doc, shown), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="check-{shown}.pdf"'
    return response


def _advance(bank: BankAccount, number: str) -> None:
    if number.isdigit() and int(number) >= bank.next_check_number:
        bank.next_check_number = int(number) + 1
        bank.save()


@login_required
@require_POST
def check_confirm(request, number):
    doc = _find(number)
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, common.ALREADY_DONE)
        return redirect(_detail_url(doc))
    if not is_to_print(doc):
        return _already_printed(request, doc)
    bank = _bank_account(doc)
    typed = request.POST.get("number", "").strip()
    if bank is None:
        messages.error(request, f"{_ledger(doc)} isn't set up as a bank account yet.")
        return redirect(_print_url(doc))
    if not typed:
        messages.error(request, "Enter a check number.")
        return redirect(_print_url(doc))
    printed = request.POST.get("result") == "yes"
    try:
        with transaction.atomic():
            if not common._claim_token(token):
                messages.info(request, common.ALREADY_DONE)
                return redirect(_detail_url(doc))
            checknumbers.assert_free(_ledger(doc), typed, error=PrintError)
            checknumbers.record(
                _ledger(doc), typed, CheckStatus.PRINTED if printed else CheckStatus.SPOILED,
                vendor_payment=None if _is_refund(doc) else doc, refund=doc if _is_refund(doc) else None,
                user=request.user)
            if printed:
                _set_number(doc, typed)
            _advance(bank, typed)
    except PrintError as exc:
        messages.error(request, str(exc))
        return redirect(_print_url(doc, typed))
    if printed:
        messages.success(request, f"Check {typed} for {_party(doc)} is recorded as printed.")
        return redirect(reverse("banking:checks"))
    messages.info(request, f"Check {typed} is marked spoiled and won't be used again. Print on the next number.")
    return redirect(_print_url(doc))


@login_required
@require_POST
def check_reprint(request, number):
    require_admin(request.user)
    doc = _find(number)
    row = CheckNumber.objects.filter(
        **({"refund": doc} if _is_refund(doc) else {"vendor_payment": doc}), status=CheckStatus.PRINTED).first()
    if row is None:
        messages.error(request, f"{doc.number} has not been printed from here, so there is nothing to reprint.")
        return redirect(_detail_url(doc))
    locked = lock_message(doc.journal_entry) if doc.journal_entry_id else None
    if locked:
        messages.error(request, locked)
        return redirect(_detail_url(doc))
    with transaction.atomic():
        row.status = CheckStatus.SPOILED
        row.save()
        _set_number(doc, "")
    messages.info(request, f"Check {row.number} is marked spoiled. Print on a new number.")
    return redirect(_print_url(doc))


# ---- layout ----

def _money(value: str) -> Decimal:
    try:
        return Decimal(value or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        raise LayoutError("Offsets must be numbers like 0.10.") from None


@login_required
def layout_page(request, account_id):
    require_admin(request.user)
    bank = get_object_or_404(BankAccount.objects.select_related("ledger_account"), pk=account_id)
    context = {"account": bank, "labels": FIELD_LABELS, "nav_active": "checks"}
    if request.method == "POST":
        post = request.POST
        fields = {
            name: {"x": post.get(f"x-{name}", ""), "y": post.get(f"y-{name}", ""), "size": post.get(f"size-{name}", ""),
                   "visible": post.get(f"visible-{name}") == "on"}
            for name in DEFAULT_LAYOUT
        }
        try:
            save_layout(bank, fields=fields, offset_x=_money(post.get("offset_x", "")),
                        offset_y=_money(post.get("offset_y", "")), user=request.user)
        except LayoutError as exc:
            rows = [{"name": n, "label": FIELD_LABELS[n], **fields[n]} for n in DEFAULT_LAYOUT]
            return render(request, "banking/layout.html", {
                **context, "rows": rows, "offset_x": post.get("offset_x", ""), "offset_y": post.get("offset_y", ""),
                "errors": [str(exc)]})
        messages.success(request, "Saved the check layout.")
        return redirect(reverse("banking:layout", args=[bank.pk]))
    layout = layout_for(bank)
    rows = [{"name": n, "label": FIELD_LABELS[n], **layout["fields"][n]} for n in DEFAULT_LAYOUT]
    return render(request, "banking/layout.html", {
        **context, "rows": rows, "offset_x": f"{layout['offset_x']:.2f}", "offset_y": f"{layout['offset_y']:.2f}"})


@login_required
def calibrate_pdf(request, account_id):
    require_admin(request.user)
    bank = get_object_or_404(BankAccount, pk=account_id)
    response = HttpResponse(calibration_pdf(bank), content_type="application/pdf")
    response["Content-Disposition"] = 'inline; filename="check-calibration.pdf"'
    return response

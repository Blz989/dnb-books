from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse

from core.roles import require_admin
from ledger.errors import PostingError
from sales.errors import SalesError
from sales.numbering import _sequence, set_sequence

from ..forms import PurchasingSettingsForm
from ..models import PURCHASING_DOC_TYPES, PurchasingSettings, Threshold1099

YEAR_ERROR = "Enter a year like 2026."
AMOUNT_ERROR = "Enter an amount above zero."


def _numbering_rows(post=None, errors=None):
    rows = []
    for doc_type in PURCHASING_DOC_TYPES:
        seq = _sequence(doc_type)
        rows.append({
            "type": doc_type.value, "label": doc_type.label,
            "prefix": post.get(f"prefix_{doc_type.value}", seq.prefix) if post else seq.prefix,
            "next_number": post.get(f"next_{doc_type.value}", seq.next_number) if post else seq.next_number,
            "error": (errors or {}).get(doc_type.value, ""),
        })
    return rows


def _amount(raw: str) -> Decimal | None:
    try:
        value = Decimal((raw or "").strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -2:
        return None
    return value


def _threshold_rows(post=None, errors=None):
    return [
        {"year": t.year, "amount": post.get(f"amount_{t.year}", t.amount) if post else t.amount,
         "error": (errors or {}).get(str(t.year), "")}
        for t in Threshold1099.objects.order_by("year")
    ]


@login_required
def purchasing_settings(request):
    require_admin(request.user)
    config = PurchasingSettings.load()
    form = PurchasingSettingsForm(instance=config)
    numbering_errors: dict[str, str] = {}
    threshold_errors: dict[str, str] = {}
    numbering_post = threshold_post = None
    new_row = {"year": "", "amount": "", "error": ""}

    if request.method == "POST" and request.POST.get("action") == "settings":
        form = PurchasingSettingsForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect(reverse("purchasing:settings"))
    elif request.method == "POST" and request.POST.get("action") == "numbering":
        numbering_post = request.POST
        for doc_type in PURCHASING_DOC_TYPES:
            seq = _sequence(doc_type)
            prefix = numbering_post.get(f"prefix_{doc_type.value}", seq.prefix).strip()
            raw = numbering_post.get(f"next_{doc_type.value}", "").strip()
            if not raw.isdigit() or int(raw) < 1:
                numbering_errors[doc_type.value] = "Enter a next number of 1 or more."
                continue
            if prefix == seq.prefix and int(raw) == seq.next_number:
                continue
            try:
                set_sequence(doc_type, prefix=prefix, next_number=int(raw), user=request.user)
            except (SalesError, PostingError) as exc:
                numbering_errors[doc_type.value] = str(exc)
        if not numbering_errors:
            messages.success(request, "Numbering saved.")
            return redirect(reverse("purchasing:settings"))
    elif request.method == "POST" and request.POST.get("action") == "thresholds":
        threshold_post = request.POST
        updates: dict[int, Decimal] = {}
        for t in Threshold1099.objects.all():
            amount = _amount(threshold_post.get(f"amount_{t.year}", ""))
            if amount is None:
                threshold_errors[str(t.year)] = AMOUNT_ERROR
            else:
                updates[t.year] = amount
        new_year, new_amount = threshold_post.get("new_year", "").strip(), threshold_post.get("new_amount", "").strip()
        new_row = {"year": new_year, "amount": new_amount, "error": ""}
        if new_year or new_amount:
            if not (new_year.isdigit() and 2000 <= int(new_year) <= 2100):
                new_row["error"] = YEAR_ERROR
            elif _amount(new_amount) is None:
                new_row["error"] = AMOUNT_ERROR
            else:
                updates[int(new_year)] = _amount(new_amount)
        if not threshold_errors and not new_row["error"]:
            for year, amount in updates.items():
                Threshold1099.objects.update_or_create(year=year, defaults={"amount": amount})
            messages.success(request, "1099 thresholds saved.")
            return redirect(reverse("purchasing:settings"))

    return render(request, "purchasing/settings.html", {
        "form": form, "numbering": _numbering_rows(numbering_post, numbering_errors),
        "thresholds": _threshold_rows(threshold_post, threshold_errors), "new_row": new_row,
        "nav_active": "purchasing-settings",
    })

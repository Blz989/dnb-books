from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from core.roles import is_admin
from ..forms import DepositForm
from ..models import Deposit, DocStatus
from ..posting.deposits import make_deposit, undeposited_payments, void_deposit
from . import common
from .receipts import ERRORS, _void_screen

PAGE_SIZE = 50


@login_required
def deposit_list(request):
    page = Paginator(Deposit.objects.select_related("bank_account"), PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [{"deposit": d, "count": d.all_payments.count(), "total": sum((p.amount for p in d.all_payments.all()), 0)}
            for d in page]
    return render(request, "sales/deposit_list.html", {"rows": rows, "page": page, "nav_active": "deposits"})


def _new_context(form, token, payments, selected, **extra):
    return {"form": form, "token": token, "payments": payments, "selected": selected, "nav_active": "deposits", **extra}


@login_required
def deposit_new(request):
    payments = list(undeposited_payments())
    if request.method != "POST":
        form = DepositForm(initial={"date": timezone.localdate().isoformat()})
        return render(request, "sales/deposit_new.html", _new_context(form, common.new_token(), payments, set()))
    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, "This deposit was already saved.")
        return redirect(reverse("sales:deposit-list"))
    form = DepositForm(request.POST)
    ids = {int(i) for i in request.POST.getlist("payments") if i.isdigit()}
    chosen = [p for p in payments if p.pk in ids]
    errors = [] if chosen else ["Choose at least one payment."]
    if form.is_valid() and not errors:
        d = form.cleaned_data
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, "This deposit was already saved.")
                    return redirect(reverse("sales:deposit-list"))
                deposit = make_deposit(payments=chosen, bank_account=d["bank_account"], date=d["date"], user=request.user)
        except ERRORS as exc:
            return render(request, "sales/deposit_new.html", _new_context(form, token, payments, ids, **common.error_context(exc)))
        messages.success(request, f"Saved {deposit.number}.")
        return redirect(reverse("sales:deposit-detail", args=[deposit.number]))
    return render(request, "sales/deposit_new.html", _new_context(form, token, payments, ids, errors=errors))


@login_required
def deposit_detail(request, number):
    deposit = Deposit.objects.filter(number=number).select_related("bank_account", "journal_entry").first()
    if deposit is None:
        raise Http404("No deposit has that number.")
    payments = list(deposit.all_payments.select_related("customer").order_by("date", "id"))
    return render(request, "sales/deposit_detail.html", {
        "deposit": deposit, "payments": payments, "total": sum((p.amount for p in payments), 0),
        "is_void": deposit.status == DocStatus.VOID, "is_admin": is_admin(request.user), "nav_active": "deposits"})


@login_required
def deposit_void(request, number):
    deposit = Deposit.objects.filter(number=number).first()
    if deposit is None:
        raise Http404("No deposit has that number.")
    return _void_screen(request, deposit, "deposit", void_deposit, reverse("sales:deposit-detail", args=[number]), "deposits")

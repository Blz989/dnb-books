from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from core.roles import is_admin, require_admin
from ledger.errors import PostingError

from ..forms import TransferForm
from ..inbox import InboxError
from ..models import AccountKind, BankAccount, BankTransaction, Transfer, TransferKind, TransferStatus, TxnStatus
from ..transfers import TransferError, create_transfer, void_transfer
from . import common

ERRORS = (TransferError, InboxError, PostingError)
PAGE_SIZE = 50


@login_required
def transfer_list(request):
    page = Paginator(Transfer.objects.select_related("from_account__ledger_account", "to_account__ledger_account"),
                     PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "banking/transfers.html", {"page": page, "nav_active": "transfers"})


def _initial_from_txn(txn: BankTransaction) -> dict:
    """An outgoing row is the 'from' side; an incoming row is the 'to' side."""
    initial = {"from_txn": txn.pk, "date": txn.date.isoformat(), "amount": f"{abs(txn.amount):.2f}"}
    if txn.amount < 0:
        initial["from_account"] = txn.bank_account_id
    else:
        initial["to_account"] = txn.bank_account_id
    kind = {AccountKind.CARD: TransferKind.CARD_PAYOFF, AccountKind.LOAN: TransferKind.LOAN_PAYMENT}.get(
        txn.bank_account.kind if txn.amount >= 0 else None, TransferKind.TRANSFER)
    initial["kind"] = kind
    return initial


@login_required
def transfer_new(request):
    context = {"nav_active": "transfers", "loan_kind": TransferKind.LOAN_PAYMENT}
    if request.method != "POST":
        initial = {"date": ""}
        from_txn = request.GET.get("from_txn", "")
        txn = BankTransaction.objects.filter(pk=int(from_txn), status=TxnStatus.NEW).select_related(
            "bank_account").first() if from_txn.isdigit() else None
        if txn is not None:
            initial = _initial_from_txn(txn)
            context["txn"] = txn
        return render(request, "banking/transfer_form.html", {
            **context, "form": TransferForm(initial=initial), "token": common.new_token()})

    token = request.POST.get("token", "")
    if common._token_used(token):
        messages.info(request, common.ALREADY_DONE)
        return redirect(reverse("banking:transfers"))
    form = TransferForm(request.POST)
    if form.is_valid():
        d = form.cleaned_data
        from_txn = BankTransaction.objects.filter(pk=d["from_txn"]).first() if d["from_txn"] else None
        try:
            with transaction.atomic():
                if not common._claim_token(token):
                    messages.info(request, common.ALREADY_DONE)
                    return redirect(reverse("banking:transfers"))
                transfer = create_transfer(
                    date=d["date"], from_account=d["from_account"], to_account=d["to_account"], amount=d["amount"],
                    kind=d["kind"], user=request.user, principal=d["principal"], interest=d["interest"],
                    interest_account=d["interest_account"], from_txn=from_txn)
        except ERRORS as exc:
            return render(request, "banking/transfer_form.html", {
                **context, "form": form, "token": token, "errors": [str(exc)]})
        messages.success(request, f"Saved TR-{transfer.pk}.")
        return redirect(reverse("banking:inbox") if from_txn else reverse("banking:transfers"))
    return render(request, "banking/transfer_form.html", {**context, "form": form, "token": token})


@login_required
def transfer_void(request, pk):
    require_admin(request.user)
    transfer = get_object_or_404(Transfer.objects.select_related("from_account__ledger_account", "to_account__ledger_account"), pk=pk)
    if transfer.status == TransferStatus.VOID:
        messages.info(request, f"TR-{transfer.pk} is already void.")
        return redirect(reverse("banking:transfers"))
    if request.method == "POST":
        try:
            void_transfer(transfer, user=request.user, reason=request.POST.get("reason", ""))
        except ERRORS as exc:
            return render(request, "banking/transfer_void.html", {
                "transfer": transfer, "errors": [str(exc)], "nav_active": "transfers"})
        messages.success(request, f"Voided TR-{transfer.pk}.")
        return redirect(reverse("banking:transfers"))
    return render(request, "banking/transfer_void.html", {"transfer": transfer, "nav_active": "transfers"})

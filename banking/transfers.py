"""Transfers between DNB's own accounts, card payoffs, and loan payments."""
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction

from core.roles import require_admin
from ledger.models import Account
from ledger.periods import assert_open
from ledger.posting import LineSpec, post_entry, void_entry

from . import inbox
from .models import AccountKind, BankAccount, BankTransaction, Transfer, TransferKind, TransferStatus

ZERO = Decimal("0.00")


class TransferError(Exception):
    """A problem a person can fix. Messages are plain language."""


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


def _lines(kind, from_account, to_account, amount, principal, interest, interest_account) -> list[LineSpec]:
    source = from_account.ledger_account
    if kind != TransferKind.LOAN_PAYMENT:
        return [LineSpec(to_account.ledger_account, debit=amount), LineSpec(source, credit=amount)]
    lines = []
    if principal > 0:
        lines.append(LineSpec(to_account.ledger_account, debit=principal))
    if interest > 0:
        lines.append(LineSpec(interest_account, debit=interest))
    lines.append(LineSpec(source, credit=amount))
    return lines


def create_transfer(
    *, date: date_type, from_account: BankAccount, to_account: BankAccount, amount: Decimal, kind: TransferKind,
    user, principal: Decimal | None = None, interest: Decimal | None = None, interest_account: Account | None = None,
    from_txn: BankTransaction | None = None,
) -> Transfer:
    if from_account.pk == to_account.pk:
        raise TransferError("Choose two different accounts.")
    if amount is None or amount <= 0:
        raise TransferError("Enter an amount above zero.")
    if kind == TransferKind.CARD_PAYOFF and to_account.kind != AccountKind.CARD:
        raise TransferError("A card payoff goes to a credit card account.")
    if kind == TransferKind.LOAN_PAYMENT:
        if to_account.kind != AccountKind.LOAN:
            raise TransferError("A loan payment goes to a loan account.")
        principal, interest = principal or ZERO, interest or ZERO
        if principal < 0 or interest < 0 or principal + interest != amount:
            raise TransferError(f"Principal and interest must add up to {_money(amount)}.")
        if interest > 0 and interest_account is None:
            raise TransferError("Choose the interest account.")
    else:
        principal = interest = interest_account = None
    if from_txn is not None and from_txn.bank_account_id not in (from_account.pk, to_account.pk):
        raise TransferError("That bank transaction isn't on either account.")

    with transaction.atomic():
        assert_open(date)
        transfer = Transfer.objects.create(
            date=date, from_account=from_account, to_account=to_account, amount=amount, kind=kind,
            principal=principal, interest=interest, interest_account=interest_account, created_by=user)
        entry = post_entry(
            date=date, user=user, memo=f"{TransferKind(kind).label}: {from_account} to {to_account}",
            source_type="banking.transfer", source_id=f"TR-{transfer.pk}",
            lines=_lines(kind, from_account, to_account, amount, principal, interest, interest_account))
        transfer.journal_entry = entry
        transfer.save()
        if from_txn is not None:
            inbox.link(from_txn, entry, user=user)
    return transfer


def void_transfer(transfer: Transfer, *, user, reason: str, date: date_type | None = None) -> Transfer:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise TransferError("Enter a reason.")
    with transaction.atomic():
        current = Transfer.objects.select_related("journal_entry").get(pk=transfer.pk)
        if current.status == TransferStatus.VOID:
            raise TransferError(f"{current} is already void.")
        when = date or current.date
        if when < current.date:
            raise TransferError("The void date can't be before the transfer date.")
        void_entry(current.journal_entry, user=user, reason=reason, date=when)
        current.status, current.voided_on, current.void_reason = TransferStatus.VOID, when, reason
        current.save()
    transfer.refresh_from_db()
    return transfer

"""Deposits: checks and cash move from Undeposited Funds to a bank account in one batch."""
from collections.abc import Sequence
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, QuerySet

from core.roles import require_admin
from ledger.models import Account, AccountSubtype
from ledger.periods import assert_open
from ledger.posting import LineSpec, post_entry, void_entry

from ..errors import DepositError
from ..models import Deposit, DocStatus, DocType, Payment, PaymentMethod, SalesSettings
from ..numbering import next_number

CASH_METHODS = (PaymentMethod.CHECK, PaymentMethod.CASH)


def undeposited_payments() -> QuerySet[Payment]:
    return (
        Payment.objects.filter(status=DocStatus.OPEN, method__in=CASH_METHODS)
        .filter(Q(deposit__isnull=True) | Q(deposit__status=DocStatus.VOID))
        .select_related("customer")
        .order_by("date", "id")
    )


def make_deposit(*, payments: Sequence[Payment], bank_account: Account, date: date_type, user) -> Deposit:
    with transaction.atomic():
        settings = SalesSettings.load().require_configured()
        if not payments:
            raise DepositError("Choose at least one payment.")
        if bank_account.subtype != AccountSubtype.BANK:
            raise DepositError("Choose the bank account for the deposit.")
        current = list(Payment.objects.select_related("deposit").filter(pk__in=[p.pk for p in payments]))
        for payment in sorted(current, key=lambda p: (p.date, p.pk)):
            if payment.status == DocStatus.VOID:
                raise DepositError(f"{payment.number} is void.")
            if payment.method not in CASH_METHODS:
                raise DepositError(
                    f"{payment.number} was paid by {PaymentMethod(payment.method).label} and went straight to the bank.")
            if payment.deposit_id and payment.deposit.status != DocStatus.VOID:
                raise DepositError(f"{payment.number} is already in deposit {payment.deposit.number}.")
            if date < payment.date:
                raise DepositError(f"The deposit date can't be before {payment.number}'s date.")
            # A re-deposit dated before the earlier deposit's void would put the payment in two deposits at once.
            for earlier in payment.all_deposits.filter(status=DocStatus.VOID, voided_on__gt=date):
                raise DepositError(
                    f"The deposit date can't be before {earlier.voided_on.isoformat()}, "
                    f"when {payment.number} was taken out of {earlier.number}.")
        total = sum((p.amount for p in current), Decimal("0.00"))
        number = next_number(DocType.DEP)
        entry = post_entry(
            date=date, user=user, memo=f"Deposit {number}", source_type="sales.deposit", source_id=number,
            lines=[LineSpec(bank_account, debit=total), LineSpec(settings.undeposited_account, credit=total)],
        )
        deposit = Deposit.objects.create(
            number=number, date=date, bank_account=bank_account, journal_entry=entry, created_by=user)
        Payment.objects.filter(pk__in=[p.pk for p in current]).update(deposit=deposit)
        deposit.all_payments.set(current)
        return deposit


def void_deposit(deposit: Deposit, *, user, reason: str, date: date_type | None = None) -> Deposit:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise DepositError("Enter a reason.")
    with transaction.atomic():
        current = Deposit.objects.select_related("journal_entry").get(pk=deposit.pk)
        if current.status == DocStatus.VOID:
            raise DepositError(f"{current.number} is already void.")
        when = date or current.date
        if when < current.date:
            raise DepositError(f"The void date can't be before {current.number}'s date.")
        assert_open(when)
        void_entry(current.journal_entry, user=user, reason=reason, date=when)
        # The payments keep pointing at the void deposit and show as undeposited again.
        current.status = DocStatus.VOID
        current.voided_on = when
        current.void_reason = reason
        current.save()
    deposit.refresh_from_db()
    return deposit

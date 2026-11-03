"""Undeposited payments that an existing ledger entry already took out of Undeposited Funds.

Used for history imported from Zoho, where cash was sometimes cleared by a manual journal or paid out
of Undeposited Funds instead of being deposited. This writes nothing to the ledger."""
from collections.abc import Sequence
from decimal import Decimal

from django.db import transaction

from ledger.models import EntryStatus, JournalEntry

from ..errors import DepositError
from ..models import Deposit, DocStatus, Payment, SalesSettings, UndepositedClearing
from .deposits import CASH_METHODS

ZERO = Decimal("0.00")


def taken_out_of_undeposited(entry: JournalEntry) -> Decimal:
    settings = SalesSettings.load()
    return sum(
        (l.credit - l.debit for l in entry.lines.all() if l.account_id == settings.undeposited_account_id), ZERO)


def clear_with_entry(*, payments: Sequence[Payment], entry: JournalEntry, user, note: str = "") -> UndepositedClearing:
    with transaction.atomic():
        entry = JournalEntry.objects.get(pk=entry.pk)
        if entry.status != EntryStatus.POSTED:
            raise DepositError(f"{entry.display_number} is not a posted entry.")
        if UndepositedClearing.objects.filter(journal_entry=entry).exists() or Deposit.objects.filter(
                journal_entry=entry).exists():
            raise DepositError(f"{entry.display_number} already accounts for payments.")
        if not payments:
            raise DepositError("Choose at least one payment.")
        current = list(Payment.objects.select_related("deposit").filter(pk__in=[p.pk for p in payments]))
        for payment in sorted(current, key=lambda p: (p.date, p.pk)):
            if payment.status == DocStatus.VOID:
                raise DepositError(f"{payment.number} is void.")
            if payment.method not in CASH_METHODS:
                raise DepositError(f"{payment.number} went straight to the bank.")
            if payment.deposit_id and payment.deposit.status != DocStatus.VOID:
                raise DepositError(f"{payment.number} is already in deposit {payment.deposit.number}.")
            if payment.clearing_id and payment.clearing.journal_entry.status == EntryStatus.POSTED:
                raise DepositError(f"{payment.number} is already cleared by {payment.clearing.journal_entry.display_number}.")
            if payment.date > entry.date:
                raise DepositError(f"{payment.number} is dated after {entry.display_number}.")
        total = sum((p.amount for p in current), ZERO)
        taken = taken_out_of_undeposited(entry)
        if total != taken:
            raise DepositError(
                f"The payments add up to {total:,.2f} but the entry takes out {taken:,.2f} from Undeposited Funds.")
        clearing = UndepositedClearing.objects.create(journal_entry=entry, note=note[:200], created_by=user)
        Payment.objects.filter(pk__in=[p.pk for p in current]).update(clearing=clearing)
        return clearing

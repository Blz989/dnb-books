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


def clear_with_entry(
    *, payments: Sequence[Payment], entry: JournalEntry, user, note: str = "",
    other_entries: Sequence[JournalEntry] = (),
) -> UndepositedClearing:
    """other_entries: entries whose net debit to Undeposited Funds the clearing entry also paid out; the payments
    plus those debits must equal what the clearing entry takes out, to the cent."""
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
        others = list(JournalEntry.objects.filter(pk__in={o.pk for o in other_entries}))
        claimed_others = set(UndepositedClearing.objects.filter(
            other_entries__in=others).values_list("other_entries", flat=True))
        for other in others:
            if other.pk == entry.pk:
                raise DepositError(f"{other.display_number} can't be both the clearing entry and an offsetting one.")
            if other.status != EntryStatus.POSTED:
                raise DepositError(f"{other.display_number} is not a posted entry.")
            if other.date > entry.date:
                raise DepositError(f"{other.display_number} is dated after {entry.display_number}.")
            if other.pk in claimed_others or UndepositedClearing.objects.filter(journal_entry=other).exists():
                raise DepositError(f"{other.display_number} already accounts for payments.")
            debit = -taken_out_of_undeposited(other)
            if debit <= 0:
                raise DepositError(f"{other.display_number} does not put money into Undeposited Funds.")
            total += debit
        taken = taken_out_of_undeposited(entry)
        if total != taken:
            raise DepositError(
                f"The payments add up to {total:,.2f} but the entry takes out {taken:,.2f} from Undeposited Funds.")
        clearing = UndepositedClearing.objects.create(journal_entry=entry, note=note[:200], created_by=user)
        clearing.other_entries.set(others)
        Payment.objects.filter(pk__in=[p.pk for p in current]).update(clearing=clearing)
        return clearing

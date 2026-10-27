from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied
from django.db.models import Sum

from ledger.errors import EntryStateError, PeriodClosedError
from ledger.models import EntryStatus, JournalLine
from ledger.periods import close_period
from ledger.posting import void_entry


def balance_for(account):
    t = JournalLine.objects.in_ledger().filter(account=account).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (t["d"] or Decimal("0")) - (t["c"] or Decimal("0"))


def test_void_nets_to_zero(admin_user, posted_entry, cash):
    rev = void_entry(posted_entry, user=admin_user, reason="Duplicate")
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.VOID and rev.reverses == posted_entry
    assert rev.status == EntryStatus.POSTED
    assert balance_for(cash) == Decimal("0.00")


def test_reversal_swaps_sides_and_copies_source(admin_user, posted_entry, cash, revenue):
    rev = void_entry(posted_entry, user=admin_user, reason="Duplicate")
    assert rev.memo == f"Void of {posted_entry.display_number}: Duplicate"
    assert rev.date == posted_entry.date
    assert rev.lines.get(account=cash).credit == Decimal("100.00")
    assert rev.lines.get(account=revenue).debit == Decimal("100.00")


def test_void_in_closed_month_needs_open_date(admin_user, posted_entry):  # Review Focus 3
    close_period(posted_entry.date.year, posted_entry.date.month, admin_user)
    with pytest.raises(PeriodClosedError, match="January 2026 is closed"):
        void_entry(posted_entry, user=admin_user, reason="x")
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.POSTED
    rev = void_entry(posted_entry, user=admin_user, reason="x", date=date(2026, 2, 1))
    assert rev.date == date(2026, 2, 1)


def test_cannot_void_twice(admin_user, posted_entry):
    void_entry(posted_entry, user=admin_user, reason="x")
    with pytest.raises(EntryStateError, match="Only posted entries can be voided."):
        void_entry(posted_entry, user=admin_user, reason="again")


def test_cannot_void_draft(admin_user, draft_entry):
    with pytest.raises(EntryStateError, match="Only posted entries can be voided."):
        void_entry(draft_entry, user=admin_user, reason="x")


def test_standard_user_cannot_void(standard_user, posted_entry):
    with pytest.raises(PermissionDenied):
        void_entry(posted_entry, user=standard_user, reason="x")
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.POSTED


def test_reason_required(admin_user, posted_entry):
    with pytest.raises(ValueError, match="A reason is required to void an entry."):
        void_entry(posted_entry, user=admin_user, reason="  ")

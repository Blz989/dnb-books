from datetime import date

import pytest
from django.core.exceptions import PermissionDenied

from ledger.errors import PeriodClosedError
from ledger.periods import assert_open, close_period, is_closed, reopen_period


def test_new_period_is_open(db):
    assert not is_closed(date(2026, 3, 15))


def test_close_then_assert_open_raises(admin_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(PeriodClosedError, match="March 2026 is closed"):
        assert_open(date(2026, 3, 31))
    assert_open(date(2026, 4, 1))  # next month unaffected


def test_closed_message_tells_user_what_to_do(admin_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(PeriodClosedError, match="Choose a date in an open month or ask an admin to reopen it."):
        assert_open(date(2026, 3, 1))


def test_close_records_who_and_when(admin_user):
    p = close_period(2026, 3, admin_user)
    assert p.is_closed and p.closed_by == admin_user and p.closed_at is not None


def test_standard_user_cannot_close(standard_user):
    with pytest.raises(PermissionDenied):
        close_period(2026, 3, standard_user)


def test_standard_user_cannot_reopen(admin_user, standard_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(PermissionDenied):
        reopen_period(2026, 3, standard_user, "please")


def test_reopen_requires_reason(admin_user):
    close_period(2026, 3, admin_user)
    with pytest.raises(ValueError, match="A reason is required to reopen a period."):
        reopen_period(2026, 3, admin_user, "  ")
    p = reopen_period(2026, 3, admin_user, "Late vendor bill")
    assert not p.is_closed and p.reopen_reason == "Late vendor bill"
    assert not is_closed(date(2026, 3, 10))

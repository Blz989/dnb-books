import calendar
from datetime import date

from django.utils import timezone

from core.roles import require_admin

from .errors import PeriodCloseBlockedError, PeriodClosedError
from .models import AccountingPeriod


# Other apps register checks that must pass before a month closes, e.g. items checks that stock value
# matches the inventory accounts. Each check takes (year, month) and returns problem messages.
_close_checks = []


def register_close_check(check) -> None:
    if check not in _close_checks:
        _close_checks.append(check)


def get_period(d: date) -> AccountingPeriod:
    period, _ = AccountingPeriod.objects.get_or_create(year=d.year, month=d.month)
    return period


def is_closed(d: date) -> bool:
    return AccountingPeriod.objects.filter(year=d.year, month=d.month, is_closed=True).exists()


def assert_open(d: date) -> None:
    if is_closed(d):
        raise PeriodClosedError(
            f"{calendar.month_name[d.month]} {d.year} is closed. "
            "Choose a date in an open month or ask an admin to reopen it."
        )


def close_period(year: int, month: int, user) -> AccountingPeriod:
    require_admin(user)
    problems = [p for check in _close_checks for p in check(year, month)]
    if problems:
        raise PeriodCloseBlockedError(" ".join(problems))
    period = get_period(date(year, month, 1))
    period.is_closed = True
    period.closed_by = user
    period.closed_at = timezone.now()
    period.save()
    return period


def reopen_period(year: int, month: int, user, reason: str) -> AccountingPeriod:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to reopen a period.")
    period = get_period(date(year, month, 1))
    period.is_closed = False
    period.reopen_reason = reason
    period.save()
    return period

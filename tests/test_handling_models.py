from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from handling.models import HandlingRate, HandlingRun, HandlingSettings, RunStatus
from handling.rates import add_rate, rate_for


def test_settings_not_configured_until_vendor_account_start_and_rate(db, admin_user, vendor, expense):
    s = HandlingSettings.load()
    assert not s.is_configured
    s.vendor = vendor
    s.expense_account = expense
    s.save()
    assert not s.is_configured
    s.start_date = date(2026, 10, 1)
    s.save()
    assert not s.is_configured
    add_rate(effective_from=date(2026, 10, 1), percent=Decimal("55"), user=admin_user)
    assert HandlingSettings.load().is_configured


def test_rate_for_uses_latest_effective_on_or_before(db, admin_user):
    add_rate(effective_from=date(2026, 10, 1), percent=Decimal("55"), user=admin_user)
    add_rate(effective_from=date(2026, 11, 1), percent=Decimal("50"), user=admin_user)
    assert rate_for(date(2026, 10, 31)) == Decimal("0.5500")
    assert rate_for(date(2026, 11, 1)) == Decimal("0.5000")
    assert rate_for(date(2026, 9, 30)) is None


def test_add_rate_stores_fraction(db, admin_user):
    r = add_rate(effective_from=date(2026, 10, 1), percent=Decimal("55"), user=admin_user)
    r.refresh_from_db()
    assert r.rate == Decimal("0.5500")


@pytest.mark.parametrize("bad", ["-1", "101"])
def test_add_rate_refuses_bad_percent(db, admin_user, bad):
    with pytest.raises(ValidationError, match="Enter a rate between 0 and 100%."):
        add_rate(effective_from=date(2026, 10, 1), percent=Decimal(bad), user=admin_user)
    assert not HandlingRate.objects.exists()


def test_add_rate_must_start_after_latest(db, admin_user):
    add_rate(effective_from=date(2026, 10, 1), percent=Decimal("55"), user=admin_user)
    for d in (date(2026, 10, 1), date(2026, 9, 1)):
        with pytest.raises(ValidationError, match="The new rate must start after Oct 1, 2026."):
            add_rate(effective_from=d, percent=Decimal("50"), user=admin_user)


def test_add_rate_is_admin_only(db, standard_user):
    with pytest.raises(PermissionDenied):
        add_rate(effective_from=date(2026, 10, 1), percent=Decimal("55"), user=standard_user)


def _run(admin_user, status, start=date(2026, 10, 16)):
    return HandlingRun.objects.create(
        period_start=start, period_end=date(2026, 10, 31), total=Decimal("0.00"),
        status=status, statement_pdf="handling/x.pdf", created_by=admin_user)


def test_only_one_posted_run_per_period(db, admin_user):
    _run(admin_user, RunStatus.POSTED)
    with pytest.raises(IntegrityError), transaction.atomic():
        _run(admin_user, RunStatus.POSTED)
    _run(admin_user, RunStatus.VOID)
    assert HandlingRun.objects.count() == 2


def test_run_label(db, admin_user):
    assert _run(admin_user, RunStatus.POSTED).label == "Oct 16–31, 2026"

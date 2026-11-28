from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from ledger.models import (
    EntryStatus, JournalEntry, RecurringJournal, RecurringJournalLine, RecurringJournalRun,
)
from ledger.periods import close_period
from ledger.recurring import Frequency, add_months, due_dates, next_date, occurrence, run_all
from purchasing.balances import bill_total
from purchasing.models import Bill, RecurringBill, RecurringBillLine, RecurringBillRun

D = Decimal


# ---- date math ----

def test_add_months_clamps_to_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


@pytest.mark.parametrize("freq,n,expected", [
    (Frequency.WEEKLY, 2, date(2026, 2, 14)),
    (Frequency.BIWEEKLY, 2, date(2026, 2, 28)),
    (Frequency.MONTHLY, 1, date(2026, 2, 28)),
    (Frequency.MONTHLY, 3, date(2026, 4, 30)),
    (Frequency.QUARTERLY, 1, date(2026, 4, 30)),
    (Frequency.SEMIANNUAL, 1, date(2026, 7, 31)),
    (Frequency.YEARLY, 1, date(2027, 1, 31)),
])
def test_occurrence(freq, n, expected):
    assert occurrence(date(2026, 1, 31), freq, n) == expected


def test_monthly_from_31st_comes_back_to_31st():
    dates = [occurrence(date(2026, 1, 31), Frequency.MONTHLY, n) for n in range(4)]
    assert dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


# ---- fixtures ----

@pytest.fixture
def rent_account(db):
    from ledger.models import Account, AccountSubtype, AccountType

    return Account.objects.create(number="6010", name="Rent", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)


@pytest.fixture
def rent(admin_user, vendor, purchasing_settings, rent_account):
    p = RecurringBill.objects.create(name="Rent", vendor=vendor, frequency=Frequency.MONTHLY,
                                     start_date=date(2026, 9, 1), created_by=admin_user, notes="Suite 4")
    RecurringBillLine.objects.create(profile=p, account=rent_account, description="Monthly rent", amount=D("2400.00"))
    return p


@pytest.fixture
def depreciation(admin_user, expense, cash):
    from ledger.models import Account, AccountSubtype, AccountType

    accum = Account.objects.create(number="1590", name="Accumulated depreciation", type=AccountType.ASSET,
                                   subtype=AccountSubtype.FIXED_ASSET)
    p = RecurringJournal.objects.create(name="Truck depreciation", memo="Monthly depreciation",
                                        frequency=Frequency.MONTHLY, start_date=date(2026, 10, 31),
                                        created_by=admin_user)
    RecurringJournalLine.objects.create(profile=p, account=expense, debit=D("250.00"), position=0)
    RecurringJournalLine.objects.create(profile=p, account=accum, credit=D("250.00"), position=1)
    return p


# ---- runs ----

def test_catch_up_creates_every_missed_bill_oldest_first(rent, admin_user, rent_account):
    summary = run_all(date(2026, 11, 15))
    bills = list(Bill.objects.order_by("date"))
    assert [b.date for b in bills] == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]
    assert summary.created == 3 and summary.failed == 0
    b = bills[0]
    assert b.vendor == rent.vendor and bill_total(b) == D("2400.00")
    assert b.notes == "From recurring bill Rent\nSuite 4"
    assert b.created_by == admin_user
    line = b.expense_lines.get()
    assert (line.account, line.description) == (rent_account, "Monthly rent")
    assert b.journal_entry.status == EntryStatus.POSTED
    assert list(rent.runs.order_by("scheduled_date").values_list("bill__date", flat=True)) == [b.date for b in bills]
    assert next_date(rent, date(2026, 11, 15)) == date(2026, 12, 1)


def test_second_run_creates_nothing(rent):
    run_all(date(2026, 11, 15))
    assert run_all(date(2026, 11, 15)).created == 0
    assert Bill.objects.count() == 3


def test_same_profile_and_date_is_unique(rent):
    run_all(date(2026, 9, 1))
    run = RecurringBillRun.objects.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        RecurringBillRun.objects.create(profile=rent, scheduled_date=run.scheduled_date, bill=run.bill)


def test_end_date_stops_runs(rent):
    rent.end_date = date(2026, 10, 15)
    rent.save()
    run_all(date(2026, 12, 31))
    assert Bill.objects.count() == 2
    assert next_date(rent, date(2026, 12, 31)) is None
    assert rent.status(date(2026, 12, 31)) == "Ended"


def test_paused_profile_does_not_run_and_resume_skips_passed_dates(rent):
    rent.is_active = False
    rent.save()
    assert run_all(date(2026, 11, 15)).created == 0
    assert rent.status(date(2026, 11, 15)) == "Paused"
    rent.resume(date(2026, 11, 15))
    assert due_dates(rent, date(2026, 11, 15)) == []
    assert next_date(rent, date(2026, 11, 15)) == date(2026, 12, 1)
    run_all(date(2026, 12, 1))
    assert [b.date for b in Bill.objects.all()] == [date(2026, 12, 1)]


def test_closed_month_error_stops_profile_then_recovers(rent, depreciation, admin_user):
    close_period(2026, 9, user=admin_user)
    summary = run_all(date(2026, 11, 15))
    assert summary.failed == 1
    rent.refresh_from_db()
    assert rent.last_error_date == date(2026, 9, 1)
    assert "2026-09" in rent.last_error or "closed" in rent.last_error.lower()
    assert Bill.objects.count() == 0  # stopped at the failed date; October waits
    assert JournalEntry.objects.filter(reference="Recurring").count() == 1  # the other profile still ran (Oct 31)

    from ledger.periods import reopen_period
    reopen_period(2026, 9, user=admin_user, reason="Late rent bill")
    run_all(date(2026, 11, 15))
    rent.refresh_from_db()
    assert Bill.objects.count() == 3 and rent.last_error == "" and rent.last_error_date is None


def test_inactive_account_error(rent, rent_account):
    rent_account.is_active = False
    rent_account.save()
    run_all(date(2026, 9, 1))
    rent.refresh_from_db()
    assert "inactive" in rent.last_error


def test_recurring_journal_entries(depreciation, expense):
    run_all(date(2026, 12, 31))
    entries = list(JournalEntry.objects.filter(reference="Recurring").order_by("date"))
    assert [e.date for e in entries] == [date(2026, 10, 31), date(2026, 11, 30), date(2026, 12, 31)]
    e = entries[1]
    assert e.memo == "Monthly depreciation" and e.source_type == "" and e.status == EntryStatus.POSTED
    assert sorted((l.account.number, l.debit, l.credit) for l in e.lines.all()) == [
        ("1590", D("0.00"), D("250.00")), ("6000", D("250.00"), D("0.00"))]
    assert RecurringJournalRun.objects.count() == 3


def test_command_runs_today(rent, monkeypatch):
    from django.utils import timezone

    monkeypatch.setattr(timezone, "localdate", lambda *a, **k: date(2026, 10, 2))
    call_command("run_recurring")
    assert Bill.objects.count() == 2


def test_scheduler_has_recurring_job(settings):
    from datetime import datetime

    from backup.management.commands.run_scheduler import upcoming_jobs

    settings.DNB_RECURRING_TIME = "05:00"
    jobs = upcoming_jobs(datetime(2026, 11, 28, 4, 0))
    assert (datetime(2026, 11, 28, 5, 0), "run_recurring") in jobs

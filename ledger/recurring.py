"""Recurring bills and journal entries (spec 3): dates, and the nightly run that creates and posts them."""
import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction

from .models import Frequency

log = logging.getLogger(__name__)

_DAYS = {Frequency.WEEKLY: 7, Frequency.BIWEEKLY: 14}
_MONTHS = {Frequency.MONTHLY: 1, Frequency.QUARTERLY: 3, Frequency.SEMIANNUAL: 6, Frequency.YEARLY: 12}
MAX_OCCURRENCES = 10_000  # a weekly schedule over ~190 years; only a guard against a runaway loop


def add_months(d: date, months: int) -> date:
    """Same day n months later, or that month's last day when it is shorter (Jan 31 + 1 = Feb 28)."""
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def occurrence(start: date, frequency: str, n: int) -> date:
    """The nth scheduled date; n = 0 is the start date. Counted from the start, so Jan 31 comes back to Mar 31."""
    if frequency in _DAYS:
        return start + timedelta(days=_DAYS[frequency] * n)
    return add_months(start, _MONTHS[frequency] * n)


def _created(profile) -> set[date]:
    return set(profile.runs.values_list("scheduled_date", flat=True))


def _schedule(profile, through: date | None):
    """Scheduled dates from the start, skipping those before resume_from, up to `through` and the end date."""
    for n in range(MAX_OCCURRENCES):
        d = occurrence(profile.start_date, profile.frequency, n)
        if profile.end_date and d > profile.end_date:
            return
        if through is not None and d > through:
            return
        if profile.resume_from and d < profile.resume_from:
            continue
        yield d


def due_dates(profile, today: date) -> list[date]:
    """Dates on or before today that have not been created yet, oldest first."""
    if not profile.is_active:
        return []
    done = _created(profile)
    return [d for d in _schedule(profile, today) if d not in done]


def next_date(profile, today: date) -> date | None:
    """The next date this schedule will create: a missed or failed one first, else the next one after today."""
    done = _created(profile)
    for d in _schedule(profile, None):
        if d not in done:
            return d
    return None


def upcoming(profile, after: date, through: date) -> list[date]:
    """Scheduled dates in (after, through] not yet created, for an active profile (cash forecast)."""
    if not profile.is_active:
        return []
    done = _created(profile)
    return [d for d in _schedule(profile, through) if d > after and d not in done]


# ---------------------------------------------------------------- creating documents

def _create_bill(profile, when: date):
    from purchasing.models import RecurringBillRun
    from purchasing.posting.bills import BillExpenseSpec, create_bill

    notes = f"From recurring bill {profile.name}"
    if profile.notes.strip():
        notes += "\n" + profile.notes.strip()
    bill = create_bill(
        vendor=profile.vendor, date=when, vendor_invoice_number="", user=profile.created_by,
        terms=profile.terms or None, notes=notes,
        expense_lines=[BillExpenseSpec(l.account, l.amount, l.description) for l in profile.lines.all()])
    RecurringBillRun.objects.create(profile=profile, scheduled_date=when, bill=bill)


def _create_journal(profile, when: date):
    from .models import RecurringJournalRun
    from .posting import LineSpec, post_entry

    entry = post_entry(
        date=when, user=profile.created_by, memo=profile.memo, reference="Recurring",
        lines=[LineSpec(l.account, debit=Decimal(l.debit), credit=Decimal(l.credit), description=l.description)
               for l in profile.lines.all()])
    RecurringJournalRun.objects.create(profile=profile, scheduled_date=when, entry=entry)


@dataclass
class RunSummary:
    created: int = 0
    failed: int = 0


def run_profile(profile, today: date, create) -> RunSummary:
    summary = RunSummary()
    for when in due_dates(profile, today):
        try:
            with transaction.atomic():
                if profile.runs.filter(scheduled_date=when).exists():
                    continue
                create(profile, when)
        except IntegrityError:
            continue  # another run created this date first
        except Exception as exc:  # noqa: BLE001 - any refusal stops this profile only
            profile.last_error = str(exc) or exc.__class__.__name__
            profile.last_error_date = when
            profile.save(update_fields=["last_error", "last_error_date"])
            log.warning("Recurring %s %s failed for %s: %s", profile._meta.model_name, profile.name, when, exc)
            summary.failed += 1
            return summary
        summary.created += 1
    if profile.last_error:
        profile.last_error, profile.last_error_date = "", None
        profile.save(update_fields=["last_error", "last_error_date"])
    return summary


def run_all(today: date) -> RunSummary:
    from purchasing.models import RecurringBill

    from .models import RecurringJournal

    total = RunSummary()
    for model, create in ((RecurringBill, _create_bill), (RecurringJournal, _create_journal)):
        for profile in model.objects.filter(is_active=True).order_by("start_date", "id"):
            s = run_profile(profile, today, create)
            total.created += s.created
            total.failed += s.failed
    return total


# ---------------------------------------------------------------- Home

def home_summary(now) -> dict:
    """Documents created by schedules in the last 7 days, and schedules that stopped on an error."""
    from purchasing.models import RecurringBill, RecurringBillRun

    from .models import RecurringJournal, RecurringJournalRun

    since = now - timedelta(days=7)
    recent = [{"kind": "Bill", "name": r.profile.name, "date": r.scheduled_date, "number": r.bill.number,
               "url": f"/purchasing/bills/{r.bill.number}/", "created": r.created_at}
              for r in RecurringBillRun.objects.filter(created_at__gte=since).select_related("profile", "bill")]
    recent += [{"kind": "Journal entry", "name": r.profile.name, "date": r.scheduled_date,
                "number": r.entry.display_number, "url": f"/books/journal/{r.entry.pk}/", "created": r.created_at}
               for r in RecurringJournalRun.objects.filter(created_at__gte=since).select_related("profile", "entry")]
    recent.sort(key=lambda r: (r["created"], r["date"]), reverse=True)
    errors = [{"label": "Recurring bill", "p": p, "url": f"/purchasing/recurring/{p.pk}/"}
              for p in RecurringBill.objects.filter(is_active=True).exclude(last_error="")]
    errors += [{"label": "Recurring journal entry", "p": p, "url": f"/books/recurring/{p.pk}/"}
               for p in RecurringJournal.objects.filter(is_active=True).exclude(last_error="")]
    return {"recurring_recent": recent[:10], "recurring_errors": errors}

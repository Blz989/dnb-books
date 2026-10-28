"""Checks registered with the ledger (spec 5.4, 5.5): the lock on handling bills and credits, and the month close
check. Both are registered in HandlingConfig.ready()."""
import calendar
from datetime import date

from ledger.models import JournalEntry

from .models import HandlingRun, HandlingSettings, RunStatus
from .periods import Period


def run_lock(entry: JournalEntry) -> str | None:
    """A POSTED run's bill or credit is voided only through void_run, which marks the run VOID before voiding it."""
    if entry.source_type == "purchasing.bill":
        run = HandlingRun.objects.filter(status=RunStatus.POSTED, bill__number=entry.source_id).first()
    elif entry.source_type == "purchasing.vendor_credit":
        run = HandlingRun.objects.filter(status=RunStatus.POSTED, vendor_credit__number=entry.source_id).first()
    else:
        return None
    if run is None:
        return None
    return f"{entry.source_id} is the handling bill for {run.label}. Void it from Handling."


def close_check(year: int, month: int) -> list[str]:
    start = HandlingSettings.objects.filter(pk=1).values_list("start_date", flat=True).first()
    if start is None or (year, month) < (start.year, start.month):
        return []
    first = Period.containing(date(year, month, 1))
    problems = []
    for period in (first, first.next()):
        # A period that ends before the start date is never billed (the first run is the one holding the start
        # date), so it can't hold the month open.
        if period.end < start:
            continue
        if not HandlingRun.objects.filter(status=RunStatus.POSTED, period_start=period.start).exists():
            problems.append(f"{calendar.month_name[month]} {year}: the handling bill for {period.label} "
                            "hasn't been created.")
    return problems

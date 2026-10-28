"""Half-month billing periods (spec 5.2): the 1st to the 15th, and the 16th to the last day of the month."""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from .models import HandlingRun, HandlingSettings, RunStatus


@dataclass(frozen=True, order=True)
class Period:
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start:%b} {self.start.day}–{self.end.day}, {self.end.year}"

    @classmethod
    def containing(cls, day: date) -> "Period":
        if day.day <= 15:
            return cls(day.replace(day=1), day.replace(day=15))
        last = calendar.monthrange(day.year, day.month)[1]
        return cls(day.replace(day=16), day.replace(day=last))

    def next(self) -> "Period":
        return Period.containing(self.end + timedelta(days=1))


def next_to_bill() -> Period | None:
    """The period after the latest POSTED run's, or the one holding the start date when nothing is billed yet."""
    settings = HandlingSettings.load()
    if not settings.is_configured:
        return None
    latest = HandlingRun.objects.filter(status=RunStatus.POSTED).order_by("-period_start").first()
    if latest is None:
        return Period.containing(settings.start_date)
    return Period(latest.period_start, latest.period_end).next()


def due_periods(today: date) -> list[Period]:
    period, due = next_to_bill(), []
    while period is not None and period.end <= today:
        due.append(period)
        period = period.next()
    return due

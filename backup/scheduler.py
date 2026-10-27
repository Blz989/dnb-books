"""Next-run times for the in-container scheduler (docker compose "scheduler" service)."""
from datetime import datetime, timedelta


def parse_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def next_run(now: datetime, *, hour: int, minute: int, day: int | None = None) -> datetime:
    """The first time strictly after `now` at hour:minute, on day-of-month `day` when given."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day is None:
        return candidate if candidate > now else candidate + timedelta(days=1)
    candidate = candidate.replace(day=day)
    if candidate > now:
        return candidate
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return candidate.replace(year=year, month=month)

"""Reads for the recurring schedules (books-gaps spec 3)."""
from decimal import Decimal

from django.utils import timezone

from ledger.models import RecurringJournal
from ledger.recurring import next_date
from purchasing.models import RecurringBill

from .runtime import fmt

ZERO = Decimal("0.00")


def _row(kind, p, amount, today, vendor=""):
    nxt = next_date(p, today) if p.is_active else None
    return {"kind": kind, "name": p.name, "vendor": vendor, "frequency": p.get_frequency_display(),
            "start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat() if p.end_date else "",
            "amount": fmt(amount), "next_date": nxt.isoformat() if nxt else "", "status": p.status(today),
            "last_error": p.last_error}


def list_recurring(user) -> dict:
    today = timezone.localdate()
    rows = [_row("bill", p, sum((l.amount for l in p.lines.all()), ZERO), today, p.vendor.name)
            for p in RecurringBill.objects.select_related("vendor").prefetch_related("lines", "runs")]
    rows += [_row("journal_entry", p, sum((l.debit for l in p.lines.all()), ZERO), today)
             for p in RecurringJournal.objects.prefetch_related("lines", "runs")]
    return {"schedules": rows}

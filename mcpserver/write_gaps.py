"""Write tools for the books-gaps features: bad debt write-offs and recurring schedules.

Each operation takes (user, payload, flags) and returns (number, summary), like the others in write.py, so a preview
runs it inside a rolled-back transaction and confirm_change runs it again for real.
"""
from django.utils import timezone

from ledger.models import Frequency, RecurringJournal, RecurringJournalLine
from ledger.recurring import next_date
from ledger.recurring_views import RecurringError, bill_lines, journal_lines
from purchasing.models import RecurringBill, RecurringBillLine
from sales.balances import invoice_balance
from sales.models import Invoice, SalesSettings
from sales.posting.writeoff import write_off

from . import lookup
from .runtime import Refused, fmt, money
from .write_void import _entry_lines, require_admin

KINDS = {"bill": (RecurringBill, "recurring bill"), "journal_entry": (RecurringJournal, "recurring journal entry")}


def _write_off(user, p, flags):
    require_admin(user)
    invoice = lookup.document(Invoice, p.get("invoice"), "invoice")
    amount = money(p["amount"], "amount") if p.get("amount") not in (None, "") else invoice_balance(invoice)
    day = lookup.day(p.get("date"), "write-off date", timezone.localdate())
    note = write_off(invoice, date=day, amount=amount, reason=p.get("reason") or "", user=user)
    account = SalesSettings.load().bad_debt_account
    return note.number, {
        "credit_note": note.number, "invoice": invoice.number, "customer": invoice.customer.name,
        "date": day.isoformat(), "amount": fmt(amount), "reason": note.reason,
        "bad_debt_account": f"{account.number} {account.name}", "balance_after": fmt(invoice_balance(invoice)),
        "posts": _entry_lines(note.journal_entry),
        "effects": "The order's profit, and the handling owed on it, go down by the same amount.",
    }


def _schedule_fields(p) -> dict:
    today = timezone.localdate()
    name = (p.get("name") or "").strip()
    if not name:
        raise Refused("Enter a name.")
    start = lookup.day(p.get("start_date"), "start date", today)
    end = lookup.day(p.get("end_date"), "end date", None)
    if end is not None and end < start:
        raise Refused("The end date can't be before the start date.")
    return {"name": name[:100], "frequency": lookup.choice(p.get("frequency") or "monthly", Frequency, "frequency"),
            "start_date": start, "end_date": end}


def _account_pk(value) -> str:
    return str(lookup.account(value).pk) if str(value or "").strip() else ""


def _recurring_bill(user, p, flags):
    fields = _schedule_fields(p)
    vendor = lookup.vendor(p.get("vendor"))
    rows = [{"account": _account_pk(l.get("account")), "description": (l.get("description") or "").strip(),
             "amount": str(l.get("amount") or "").strip()} for l in p.get("lines") or []]
    try:
        lines = bill_lines(rows)
    except RecurringError as exc:
        raise Refused(str(exc)) from None
    profile = RecurringBill.objects.create(
        vendor=vendor, terms=lookup.term(p["terms"]) if p.get("terms") else "", notes=p.get("notes") or "",
        created_by=user, **fields)
    for n, (account, description, amount) in enumerate(lines):
        RecurringBillLine.objects.create(profile=profile, account=account, description=description, amount=amount,
                                         position=n)
    return profile.name, _summary("bill", profile, sum(a for _, _, a in lines),
                                  [{"account": str(a), "description": d, "amount": fmt(x)} for a, d, x in lines])


def _recurring_journal(user, p, flags):
    fields = _schedule_fields(p)
    rows = [{"account": _account_pk(l.get("account")), "description": (l.get("description") or "").strip(),
             "debit": str(l.get("debit") or "").strip(), "credit": str(l.get("credit") or "").strip()}
            for l in p.get("lines") or []]
    try:
        specs = journal_lines(rows)
    except RecurringError as exc:
        raise Refused(str(exc)) from None
    profile = RecurringJournal.objects.create(memo=p.get("memo") or "", created_by=user, **fields)
    for n, s in enumerate(specs):
        RecurringJournalLine.objects.create(profile=profile, account=s.account, debit=s.debit, credit=s.credit,
                                            description=s.description, position=n)
    return profile.name, _summary("journal_entry", profile, sum(s.debit for s in specs),
                                  [{"account": str(s.account), "debit": fmt(s.debit) if s.debit else "",
                                    "credit": fmt(s.credit) if s.credit else "", "description": s.description}
                                   for s in specs])


def _summary(kind, profile, amount, lines) -> dict:
    today = timezone.localdate()
    nxt = next_date(profile, today) if profile.is_active else None
    out = {"kind": kind, "name": profile.name, "frequency": profile.get_frequency_display(),
           "start_date": profile.start_date.isoformat(),
           "end_date": profile.end_date.isoformat() if profile.end_date else "", "amount": fmt(amount),
           "next_date": nxt.isoformat() if nxt else "", "status": profile.status(today), "lines": lines,
           "runs": "Each date is created and posted early that morning, as the person who set it up."}
    if kind == "bill":
        out["vendor"] = profile.vendor.name
    return out


def _find(p):
    kind = (p.get("kind") or "").strip().lower().replace(" ", "_")
    if kind not in KINDS:
        raise Refused("Choose a kind: bill or journal_entry.")
    model, label = KINDS[kind]
    name = (p.get("name") or "").strip()
    found = list(model.objects.filter(name__iexact=name)[:2])
    if not found:
        raise Refused(f"No {label} is named {name}.")
    if len(found) > 1:
        raise Refused(f"More than one {label} is named {found[0].name}. Rename one in the app first.")
    return kind, found[0]


def _amount(kind, profile):
    lines = profile.lines.all()
    return sum((l.amount if kind == "bill" else l.debit for l in lines), 0)


def _pause(user, p, flags):
    kind, profile = _find(p)
    profile.is_active = False
    profile.save(update_fields=["is_active"])
    out = _summary(kind, profile, _amount(kind, profile), [])
    out.pop("lines")
    return profile.name, out


def _resume(user, p, flags):
    kind, profile = _find(p)
    profile.resume(timezone.localdate())
    out = _summary(kind, profile, _amount(kind, profile), [])
    out.pop("lines")
    out["note"] = "Dates that passed while it was paused are skipped."
    return profile.name, out


OPERATIONS = {
    "write_off_invoice": _write_off,
    "create_recurring_bill": _recurring_bill,
    "create_recurring_journal_entry": _recurring_journal,
    "pause_recurring": _pause,
    "resume_recurring": _resume,
}

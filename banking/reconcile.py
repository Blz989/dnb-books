"""Reconciling an account to its statement, and locking what was cleared."""
from datetime import date as date_type
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from core.roles import require_admin
from ledger.models import JournalEntry, JournalLine

from .models import BankAccount, Match, Reconciliation, ReconciliationItem, ReconStatus

ZERO = Decimal("0.00")


class ReconcileError(Exception):
    """A problem a person can fix. Messages are plain language."""


def reconciled_lines():
    """Journal lines cleared in a finished reconciliation."""
    ids = ReconciliationItem.objects.filter(
        cleared=True, reconciliation__status=ReconStatus.FINISHED).values("journal_line_id")
    return JournalLine.objects.filter(pk__in=ids)


def _matched_entry_ids(bank_account: BankAccount, through: date_type) -> set[int]:
    matches = Match.objects.filter(
        bank_transaction__bank_account=bank_account, bank_transaction__date__lte=through,
    ).select_related("sales_payment", "deposit", "refund", "vendor_payment")
    ids: set[int] = set()
    for match in matches:
        if match.journal_entry_id:
            ids.add(match.journal_entry_id)
        elif match.target is not None:
            ids.add(match.target.journal_entry_id)
    return ids


def _long_date(day: date_type) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _add_items(rec: Reconciliation) -> None:
    """Add the account's unreconciled lines dated on or before the statement date that the reconciliation lacks.

    Entries booked while a reconciliation is open (a bank fee spotted on the statement) must join it, or the
    difference can't reach 0.00.
    """
    lines = (
        JournalLine.objects.in_ledger()
        .filter(account=rec.bank_account.ledger_account, entry__date__lte=rec.statement_date)
        .exclude(pk__in=reconciled_lines().values("pk"))
        .exclude(pk__in=rec.items.values("journal_line_id"))
    )
    matched = _matched_entry_ids(rec.bank_account, rec.statement_date)
    ReconciliationItem.objects.bulk_create([
        ReconciliationItem(reconciliation=rec, journal_line=line, cleared=line.entry_id in matched)
        for line in lines
    ])


def start(bank_account: BankAccount, *, statement_date: date_type, statement_balance: Decimal, user) -> Reconciliation:
    with transaction.atomic():
        if Reconciliation.objects.filter(bank_account=bank_account, status=ReconStatus.IN_PROGRESS).exists():
            raise ReconcileError("Finish or cancel the reconciliation already in progress.")
        last = (
            Reconciliation.objects.filter(bank_account=bank_account, status=ReconStatus.FINISHED)
            .order_by("-statement_date").first()
        )
        # The opening balance chains from the previous statement, so statements must be reconciled in order.
        if last is not None and statement_date <= last.statement_date:
            raise ReconcileError(
                f"The statement date must be after {_long_date(last.statement_date)}, "
                "the date of the last finished reconciliation.")
        rec = Reconciliation.objects.create(
            bank_account=bank_account, statement_date=statement_date, statement_balance=statement_balance,
            started_by=user)
        _add_items(rec)
        return rec


def refresh(rec: Reconciliation) -> None:
    """Bring in lines booked since the reconciliation started. Only an open reconciliation changes."""
    with transaction.atomic():
        current = Reconciliation.objects.get(pk=rec.pk)
        if current.status == ReconStatus.IN_PROGRESS:
            _add_items(current)


def toggle(item: ReconciliationItem, cleared: bool) -> None:
    current = ReconciliationItem.objects.select_related("reconciliation").get(pk=item.pk)
    if current.reconciliation.status != ReconStatus.IN_PROGRESS:
        raise ReconcileError("This reconciliation is no longer in progress.")
    current.cleared = cleared
    current.save()
    item.cleared = cleared


def _signed(bank_account: BankAccount, line: JournalLine) -> Decimal:
    """Amount that moves the statement balance. Cards and loans are shown as the amount owed."""
    if bank_account.is_liability:
        return line.credit - line.debit
    return line.debit - line.credit


def opening_balance(rec: Reconciliation) -> Decimal:
    last = (
        Reconciliation.objects.filter(
            bank_account=rec.bank_account, status=ReconStatus.FINISHED, statement_date__lt=rec.statement_date)
        .order_by("-statement_date", "-id").first()
    )
    return last.statement_balance if last else ZERO


def cleared_total(rec: Reconciliation) -> Decimal:
    items = rec.items.filter(cleared=True).select_related("journal_line")
    return sum((_signed(rec.bank_account, i.journal_line) for i in items), ZERO)


def difference(rec: Reconciliation) -> Decimal | None:
    """None while the statement balance is blank (a reconciliation the Zoho import left to be filled in)."""
    if rec.statement_balance is None:
        return None
    return rec.statement_balance - (opening_balance(rec) + cleared_total(rec))


def set_statement_balance(rec: Reconciliation, balance: Decimal, user) -> None:
    with transaction.atomic():
        current = Reconciliation.objects.get(pk=rec.pk)
        if current.status != ReconStatus.IN_PROGRESS:
            raise ReconcileError("This reconciliation is no longer in progress.")
        current.statement_balance = balance
        current.save(update_fields=["statement_balance"])
    rec.statement_balance = balance


def rows_for(rec: Reconciliation) -> list[dict]:
    """Every line on the reconciliation, oldest first, shaped for the screen and the report."""
    account = rec.bank_account
    rows = []
    for item in rec.items.select_related("journal_line__entry").order_by("journal_line__entry__date", "pk"):
        line, entry = item.journal_line, item.journal_line.entry
        rows.append({
            "item_id": item.pk, "date": entry.date, "number": entry.source_id or entry.display_number,
            "description": entry.memo, "amount": abs(line.debit - line.credit),
            "is_deposit": (line.debit > line.credit) != account.is_liability, "cleared": item.cleared,
        })
    return rows


def summary(rec: Reconciliation) -> dict:
    cleared = cleared_total(rec)
    diff = difference(rec)
    return {"opening": opening_balance(rec), "cleared_total": cleared, "difference": diff,
            "cleared_balance": opening_balance(rec) + cleared}


def _context(rec: Reconciliation) -> dict:
    account = rec.bank_account
    rows = rows_for(rec)
    cleared = [r for r in rows if r["cleared"]]
    open_ = [r for r in rows if not r["cleared"]]
    return {
        "rec": rec, "account": account, **summary(rec),
        "cleared_deposits": [r for r in cleared if r["is_deposit"]],
        "cleared_payments": [r for r in cleared if not r["is_deposit"]],
        "outstanding": [r for r in open_ if not r["is_deposit"]],
        "in_transit": [r for r in open_ if r["is_deposit"]],
        "is_liability": account.is_liability,
        "now": timezone.localtime(),
    }


def render_report_html(rec: Reconciliation) -> str:
    return render_to_string("banking/reconcile_report_pdf.html", _context(rec))


def _report_pdf(rec: Reconciliation) -> bytes:
    from pathlib import Path

    from django.conf import settings
    from weasyprint import CSS, HTML

    css = Path(settings.BASE_DIR) / "banking" / "static" / "banking" / "reconcile_report.css"
    html = HTML(string=render_report_html(rec), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.write_pdf(stylesheets=[CSS(filename=str(css))])


def finish(rec: Reconciliation, *, user) -> Reconciliation:
    require_admin(user)
    with transaction.atomic():
        current = Reconciliation.objects.get(pk=rec.pk)
        if current.status != ReconStatus.IN_PROGRESS:
            raise ReconcileError("This reconciliation is no longer in progress.")
        _add_items(current)
        if current.statement_balance is None:
            raise ReconcileError("Enter the statement balance.")
        if difference(current) != ZERO:
            raise ReconcileError("The difference must be 0.00 to finish.")
        current.status, current.finished_by, current.finished_at = ReconStatus.FINISHED, user, timezone.now()
        current.save()
        name = f"reconciliation-{current.bank_account_id}-{current.statement_date:%Y-%m}-{current.pk}.pdf"
        current.report_pdf.save(name, ContentFile(_report_pdf(current)), save=True)
    rec.refresh_from_db()
    return rec


def undo(rec: Reconciliation, *, user, reason: str) -> Reconciliation:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise ReconcileError("Enter a reason.")
    with transaction.atomic():
        current = Reconciliation.objects.get(pk=rec.pk)
        latest = (
            Reconciliation.objects.filter(bank_account=current.bank_account, status=ReconStatus.FINISHED)
            .order_by("-statement_date", "-id").first()
        )
        if current.status != ReconStatus.FINISHED or latest is None or latest.pk != current.pk:
            raise ReconcileError("Only the most recent reconciliation can be undone.")
        current.status, current.undone_by, current.undone_at = ReconStatus.UNDONE, user, timezone.now()
        current.undo_reason = reason
        current.save()
    rec.refresh_from_db()
    return rec


def cancel(rec: Reconciliation, *, user) -> None:
    with transaction.atomic():
        current = Reconciliation.objects.get(pk=rec.pk)
        if current.status != ReconStatus.IN_PROGRESS:
            raise ReconcileError("Only a reconciliation in progress can be cancelled.")
        current.delete()


def entry_lock(entry: JournalEntry) -> str | None:
    """Lock check for ledger.void_entry: entries with a reconciled line can't be voided."""
    item = (
        ReconciliationItem.objects.filter(
            cleared=True, reconciliation__status=ReconStatus.FINISHED, journal_line__entry_id=entry.pk)
        .select_related("reconciliation__bank_account__ledger_account").first()
    )
    if item is None:
        return None
    rec = item.reconciliation
    number = entry.source_id or entry.display_number
    return (
        f"{number} is part of the {rec.statement_date:%B %Y} reconciliation for {rec.bank_account}. "
        "An admin must undo that reconciliation first."
    )

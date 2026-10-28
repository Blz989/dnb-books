"""Handling runs (spec 5.3, 5.4): one half-month period turned into one bill (or vendor credit) to the handling
company, and voiding the latest one.

The bill or credit is created only through the purchasing posting functions, so the ledger is touched only through
ledger/posting.py as usual. Profit figures come from handling.profit, which reads documents only.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from core.roles import require_admin
from ledger.errors import PostingError
from purchasing.errors import PurchasingError
from purchasing.posting.bills import BillExpenseSpec, create_bill, void_bill
from purchasing.posting.credits import AllowanceLineSpec, create_allowance_credit, void_vendor_credit
from sales.models import SalesSettings

from .models import HandlingRun, HandlingRunLine, HandlingSettings, RunStatus, long_date
from .periods import Period, next_to_bill
from .profit import ZERO, OrderProfit, _billed, order_profit, orders

FONT_DIR = Path(django_settings.BASE_DIR) / "core" / "static" / "core" / "fonts"
STATEMENT_CSS = Path(django_settings.BASE_DIR) / "handling" / "static" / "handling" / "statement_pdf.css"

NOT_SET_UP = "Set up handling in Purchasing settings first."


class HandlingError(Exception):
    pass


@dataclass
class RunRow:
    order: OrderProfit
    owed_to_date: Decimal
    billed_before: Decimal
    this_bill: Decimal


@dataclass
class Preview:
    period: Period
    rows: list[RunRow]
    total: Decimal


def preview(period: Period) -> Preview:
    """What a run for `period` would bill: each order's handling owed as of the period end, less what POSTED runs
    already billed. Orders with nothing to bill are left off."""
    config = HandlingSettings.load()
    found = orders(period.end, date_from=config.start_date, date_to=period.end)
    # An order billed earlier can drop out of that list (an invoice or order date edited out of range). It is
    # still computed, so whatever was billed for it comes back as a negative row instead of being lost.
    seen = {o.key for o in found}
    for key in sorted(set(_billed_keys()) - seen):
        found.append(order_profit(key, period.end))
    rows = []
    for o in found:
        this_bill = o.owed - o.billed
        if this_bill != ZERO:
            rows.append(RunRow(order=o, owed_to_date=o.owed, billed_before=o.billed, this_bill=this_bill))
    rows.sort(key=lambda r: (r.order.date, r.order.key))
    return Preview(period=period, rows=rows, total=sum((r.this_bill for r in rows), ZERO))


def _billed_keys() -> list[str]:
    keys = HandlingRunLine.objects.filter(run__status=RunStatus.POSTED).values_list("order_key", flat=True).distinct()
    return [k for k, total in _billed(keys).items() if total != ZERO]


def create_run(period: Period, *, user, today: date | None = None) -> HandlingRun:
    today = today or timezone.localdate()
    if period != Period.containing(period.start):
        raise ValueError(f"{period} is not a billing period.")
    # Transactions on this database begin IMMEDIATE (dnb/settings.py), so this block holds the write lock from its
    # first statement. A second request for the same period waits, then sees the first run in next_to_bill() and is
    # refused below. The one_posted_run_per_period constraint is the backstop.
    with transaction.atomic():
        config = HandlingSettings.load()
        if not config.is_configured:
            raise HandlingError(NOT_SET_UP)
        expected = next_to_bill()
        if period.start < expected.start:
            raise HandlingError(f"{period.label} is already billed.")
        if period.start > expected.start:
            raise HandlingError(f"Bill {expected.label} first.")
        if period.end > today:
            raise HandlingError(f"{period.label} can't be billed until {long_date(period.end)}.")

        pv = preview(period)
        bill = credit = None
        try:
            if pv.total > ZERO:
                bill = create_bill(
                    vendor=config.vendor, date=period.end, vendor_invoice_number=f"HND-{period.end:%Y%m%d}",
                    expense_lines=[BillExpenseSpec(config.expense_account, pv.total, f"Handling {period.label}")],
                    notes=f"Handling statement {period.label}", user=user,
                    # A period billed again after a void reuses its HND- number.
                    allow_duplicate=True)
            elif pv.total < ZERO:
                reason = f"Handling corrections {period.label}"
                credit = create_allowance_credit(
                    vendor=config.vendor, date=period.end, reason=reason, user=user,
                    lines=[AllowanceLineSpec(config.expense_account, -pv.total, reason)])
        except (PurchasingError, PostingError) as exc:
            raise HandlingError(str(exc)) from exc

        try:
            run = HandlingRun.objects.create(
                period_start=period.start, period_end=period.end, total=pv.total, bill=bill, vendor_credit=credit,
                status=RunStatus.POSTED, created_by=user)
        except IntegrityError as exc:
            raise HandlingError(f"{period.label} is already billed.") from exc
        HandlingRunLine.objects.bulk_create([
            HandlingRunLine(
                run=run, order_key=r.order.key, sales_order=r.order.sales_order, invoice=r.order.invoice,
                customer=r.order.customer, sales=r.order.net_sales, cost=r.order.cost, profit=r.order.profit,
                rate=r.order.rate if r.order.rate is not None else Decimal("0.0000"),
                owed_to_date=r.owed_to_date, billed_before=r.billed_before, this_bill=r.this_bill)
            for r in pv.rows
        ])
        run.statement_pdf.save(f"handling-{period.end:%Y%m%d}-{run.pk}.pdf", ContentFile(statement_pdf(run)),
                               save=True)
        return run


def void_run(run: HandlingRun, *, user, reason: str) -> HandlingRun:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise HandlingError("Enter a reason.")
    with transaction.atomic():
        current = HandlingRun.objects.select_related("bill", "vendor_credit").get(pk=run.pk)
        latest = HandlingRun.objects.filter(status=RunStatus.POSTED).order_by("-period_start").first()
        if current.status != RunStatus.POSTED or latest is None or latest.pk != current.pk:
            raise HandlingError("Only the most recent handling bill can be voided.")
        # VOID first: handling.checks.run_lock only guards documents of POSTED runs, so this lets our own void of
        # the bill or credit through. Any refusal below rolls the status back with everything else.
        current.status = RunStatus.VOID
        current.voided_by = user
        current.voided_at = timezone.now()
        current.void_reason = reason
        current.save()
        try:
            if current.bill is not None:
                void_bill(current.bill, user=user, reason=reason, date=current.bill.date)
            elif current.vendor_credit is not None:
                void_vendor_credit(current.vendor_credit, user=user, reason=reason, date=current.vendor_credit.date)
        except (PurchasingError, PostingError) as exc:
            raise HandlingError(str(exc)) from exc
    run.refresh_from_db()
    return run


# ---- statement PDF ----

def _address(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def render_statement_html(run: HandlingRun) -> str:
    company = SalesSettings.load()
    lines = list(run.lines.select_related("customer").order_by("order_key"))
    vendor = run.bill.vendor if run.bill else run.vendor_credit.vendor if run.vendor_credit else (
        HandlingSettings.load().vendor)
    context = {
        "run": run,
        "label": run.label,
        "company": company,
        "company_address": _address(company.company_address),
        "font_uri": FONT_DIR.as_uri(),
        "vendor": vendor,
        "lines": lines,
        "totals": {
            "sales": sum((l.sales for l in lines), ZERO),
            "cost": sum((l.cost for l in lines), ZERO),
            "profit": sum((l.profit for l in lines), ZERO),
            "owed_to_date": sum((l.owed_to_date for l in lines), ZERO),
            "billed_before": sum((l.billed_before for l in lines), ZERO),
        },
        "total_abs": abs(run.total),
        "period_end": long_date(run.period_end),
        "is_void": run.status == RunStatus.VOID,
    }
    return render_to_string("handling/statement_pdf.html", context)


def statement_pdf(run: HandlingRun) -> bytes:
    from weasyprint import CSS, HTML

    html = HTML(string=render_statement_html(run), base_url=Path(django_settings.BASE_DIR).as_uri() + "/")
    return html.write_pdf(stylesheets=[CSS(filename=str(STATEMENT_CSS))])

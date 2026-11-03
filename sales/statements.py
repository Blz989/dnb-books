"""Customer statement: dated events from the customer's documents, with a running balance. The closing balance is
the customer's A/R aging total for the same date. Every document is an event; a voided one gets a second,
opposite event on its void date, so a past statement never changes when a later document is voided."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from contacts.models import Customer
from ledger.balances import money

from .balances import (
    _active, _counts, credit_notes_with_balances, credit_total, credit_unapplied, invoice_balance, invoice_total,
    invoices_with_balances, payment_unapplied, payments_with_balances,
)
from .models import DocStatus
from .reports import Aging, AgingRow, OpenInvoiceRow, ar_aging, open_invoices

ZERO = Decimal("0.00")
# Same-day order: charges first, then credits, so a day never shows a dip below the true balance.
ORDER = {"Invoice": 0, "Refund": 1, "Credit note": 2, "Payment": 3}


@dataclass
class Applied:
    number: str
    url: str
    amount: Decimal


@dataclass
class StatementRow:
    date: date
    kind: str
    number: str
    url: str
    description: str
    charge: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal = ZERO
    is_void: bool = False
    applied: list[Applied] = field(default_factory=list)
    unapplied: Decimal | None = None


@dataclass
class AppliedLine:
    """A payment or credit note, at the amount it was applied to one invoice."""
    date: date
    kind: str
    number: str
    url: str
    detail: str
    amount: Decimal


@dataclass
class InvoiceBlock:
    number: str
    url: str
    date: date
    due_date: date
    total: Decimal
    applied: list[AppliedLine]
    balance: Decimal


@dataclass
class UnappliedRow:
    date: date
    kind: str
    number: str
    url: str
    detail: str
    amount: Decimal


@dataclass
class Statement:
    customer: Customer
    start: date
    end: date
    opening: Decimal
    rows: list[StatementRow]
    closing: Decimal
    aging: AgingRow
    open_invoices: list[OpenInvoiceRow] = field(default_factory=list)
    invoices: list[InvoiceBlock] = field(default_factory=list)
    unapplied: list[UnappliedRow] = field(default_factory=list)


def _events(customer: Customer, end: date) -> list[tuple[date, int, str, StatementRow]]:
    """(date, sort key, number, row) for every document, plus a reversal for each voided one."""
    docs = []
    for inv in customer.invoices.prefetch_related("lines"):
        docs.append(("Invoice", inv, invoice_total(inv), +1, f"/sales/invoices/{inv.number}/"))
    for pay in customer.payments.prefetch_related("applications__invoice", "refunds"):
        docs.append(("Payment", pay, pay.amount, -1, f"/sales/payments/{pay.number}/"))
    for note in customer.credit_notes.prefetch_related("lines", "applications__invoice", "refunds"):
        docs.append(("Credit note", note, credit_total(note), -1, f"/sales/credits/{note.number}/"))
    for ref in customer.refunds.all():
        docs.append(("Refund", ref, ref.amount, +1, f"/sales/refunds/{ref.number}/"))

    events = []
    for kind, doc, amount, sign, url in docs:
        amount = money(amount)
        detail = _detail(kind, doc)
        row = _row(doc.date, kind, doc.number, url, detail, amount, sign)
        if kind in ("Payment", "Credit note"):
            _fill_applications(row, doc, amount, end)
        events.append((doc.date, ORDER[kind], doc.number, row))
        if doc.status == DocStatus.VOID and doc.voided_on is not None:
            row = _row(doc.voided_on, f"Void of {kind.lower()}", doc.number, url, doc.void_reason, amount, -sign, True)
            events.append((doc.voided_on, 4, doc.number, row))
    return sorted(events, key=lambda e: e[:3])


def _fill_applications(row: StatementRow, doc, amount: Decimal, end: date) -> None:
    """Which invoices this payment or credit paid, as of the statement end, and what is still unapplied."""
    apps = sorted(_active(doc.applications.all(), end), key=lambda a: (a.applied_on, a.pk))
    row.applied = [Applied(a.invoice.number, f"/sales/invoices/{a.invoice.number}/", money(a.amount)) for a in apps]
    refunded = sum((r.amount for r in doc.refunds.all() if _counts(r, end)), ZERO)
    row.unapplied = money(amount - sum((a.amount for a in apps), ZERO) - refunded)


def _row(when, kind, number, url, description, amount, sign, is_void=False) -> StatementRow:
    row = StatementRow(when, kind, number, url, description, is_void=is_void)
    if sign > 0:
        row.charge = amount
    else:
        row.credit = amount
    return row


def _detail(kind: str, doc) -> str:
    if kind == "Payment":
        return f"{doc.get_method_display()} {doc.reference}".strip()
    if kind == "Credit note":
        return doc.reason
    if kind == "Refund":
        return f"{doc.get_method_display()} {doc.reference}".strip()
    return ""


def build_statement(customer: Customer, start: date, end: date) -> Statement:
    balance = ZERO
    opening = None
    rows: list[StatementRow] = []
    for when, _, _, row in _events(customer, end):
        if when > end:
            break
        if when < start:
            balance = money(balance + (row.charge or ZERO) - (row.credit or ZERO))
            continue
        if opening is None:
            opening = balance
        balance = money(balance + (row.charge or ZERO) - (row.credit or ZERO))
        row.balance = balance
        rows.append(row)
    if opening is None:
        opening = balance
    report: Aging = ar_aging(end, customer=customer)
    aging = report.rows[0] if report.rows else AgingRow(customer)
    return Statement(customer, start, end, money(opening), rows, balance, aging, open_invoices(end, customer),
                     _invoice_blocks(customer, start, end), _unapplied_rows(customer, end))


def _label(doc) -> str:
    return f"{doc.get_method_display()} {doc.reference}".strip() if hasattr(doc, "method") else doc.reason


def _invoice_blocks(customer: Customer, start: date, end: date) -> list[InvoiceBlock]:
    """Each invoice that counts at the end date and is dated in the range, has an application in the range, or is
    still open, with the payments and credits applied to it, split by the amount applied to this invoice."""
    blocks = []
    invoices = invoices_with_balances().prefetch_related("payment_applications__payment", "credit_applications__credit_note")
    for inv in invoices.filter(customer=customer).order_by("date", "id"):
        if not _counts(inv, end):
            continue
        lines = []
        for a in _active(inv.payment_applications.all(), end):
            if _counts(a.payment, end):
                lines.append((a.applied_on, a.pk, AppliedLine(a.applied_on, "Payment", a.payment.number,
                              f"/sales/payments/{a.payment.number}/", _label(a.payment), money(a.amount))))
        for a in _active(inv.credit_applications.all(), end):
            if _counts(a.credit_note, end):
                lines.append((a.applied_on, a.pk, AppliedLine(a.applied_on, "Credit note", a.credit_note.number,
                              f"/sales/credits/{a.credit_note.number}/", _label(a.credit_note), money(a.amount))))
        applied = [line for *_, line in sorted(lines, key=lambda t: t[:2])]
        balance = invoice_balance(inv, end)
        if start <= inv.date <= end or any(start <= a.date <= end for a in applied) or balance != 0:
            blocks.append(InvoiceBlock(inv.number, f"/sales/invoices/{inv.number}/", inv.date, inv.due_date,
                                       money(invoice_total(inv)), applied, balance))
    return blocks


def _unapplied_rows(customer: Customer, end: date) -> list[UnappliedRow]:
    rows = []
    for pay in payments_with_balances().filter(customer=customer):
        left = payment_unapplied(pay, end)
        if left != 0:
            rows.append(UnappliedRow(pay.date, "Payment", pay.number, f"/sales/payments/{pay.number}/", _label(pay), left))
    for note in credit_notes_with_balances().filter(customer=customer):
        left = credit_unapplied(note, end)
        if left != 0:
            rows.append(UnappliedRow(note.date, "Credit note", note.number, f"/sales/credits/{note.number}/", _label(note), left))
    return sorted(rows, key=lambda r: (r.date, r.number))

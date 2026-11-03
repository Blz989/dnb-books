"""Customer statement: dated events from the customer's documents, with a running balance. The closing balance is
the customer's A/R aging total for the same date. Every document is an event; a voided one gets a second,
opposite event on its void date, so a past statement never changes when a later document is voided."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from contacts.models import Customer
from ledger.balances import money

from .balances import credit_total, invoice_total
from .models import DocStatus
from .reports import Aging, AgingRow, OpenInvoiceRow, ar_aging, open_invoices

ZERO = Decimal("0.00")
# Same-day order: charges first, then credits, so a day never shows a dip below the true balance.
ORDER = {"Invoice": 0, "Refund": 1, "Credit note": 2, "Payment": 3}


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


def _events(customer: Customer) -> list[tuple[date, int, str, StatementRow]]:
    """(date, sort key, number, row) for every document, plus a reversal for each voided one."""
    docs = []
    for inv in customer.invoices.prefetch_related("lines"):
        docs.append(("Invoice", inv, invoice_total(inv), +1, f"/sales/invoices/{inv.number}/"))
    for pay in customer.payments.all():
        docs.append(("Payment", pay, pay.amount, -1, f"/sales/payments/{pay.number}/"))
    for note in customer.credit_notes.prefetch_related("lines"):
        docs.append(("Credit note", note, credit_total(note), -1, f"/sales/credits/{note.number}/"))
    for ref in customer.refunds.all():
        docs.append(("Refund", ref, ref.amount, +1, f"/sales/refunds/{ref.number}/"))

    events = []
    for kind, doc, amount, sign, url in docs:
        amount = money(amount)
        detail = _detail(kind, doc)
        events.append((doc.date, ORDER[kind], doc.number, _row(doc.date, kind, doc.number, url, detail, amount, sign)))
        if doc.status == DocStatus.VOID and doc.voided_on is not None:
            row = _row(doc.voided_on, f"Void of {kind.lower()}", doc.number, url, doc.void_reason, amount, -sign, True)
            events.append((doc.voided_on, 4, doc.number, row))
    return sorted(events, key=lambda e: e[:3])


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
    for when, _, _, row in _events(customer):
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
    return Statement(customer, start, end, money(opening), rows, balance, aging, open_invoices(end, customer))

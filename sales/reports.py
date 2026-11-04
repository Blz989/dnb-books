"""Sales reports. They read the documents and applications; AR aging and Undeposited Funds also show the ledger
balance they should equal."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.utils import timezone

from contacts.models import Customer
from ledger.balances import money

from .balances import (
    _counts, credit_notes_with_unapplied, credit_unapplied, invoice_balance, invoices_with_open_balance,
    payment_unapplied, payments_with_unapplied,
)
from .checks import _account_balance, customer_balances_total, undeposited_payments_total
from .models import (
    CreditNote, DocStatus, Fulfillment, Invoice, InvoiceLine, OrderStatus, Payment, SalesOrder, SalesSettings,
)
from .posting.deposits import undeposited_payments
from .posting.orders import remaining

ZERO = Decimal("0.00")
BUCKETS = ("current", "d1_30", "d31_60", "d61_90", "over_90")


@dataclass
class AgingRow:
    customer: Customer | None
    current: Decimal = ZERO
    d1_30: Decimal = ZERO
    d31_60: Decimal = ZERO
    d61_90: Decimal = ZERO
    over_90: Decimal = ZERO
    unapplied_credits: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return money(self.current + self.d1_30 + self.d31_60 + self.d61_90 + self.over_90 + self.unapplied_credits)


@dataclass
class Aging:
    as_of: date
    rows: list[AgingRow]
    totals: AgingRow
    ledger_balance: Decimal
    difference: Decimal


def bucket_for(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    if days_past_due <= 30:
        return "d1_30"
    if days_past_due <= 60:
        return "d31_60"
    if days_past_due <= 90:
        return "d61_90"
    return "over_90"


@dataclass
class AgedInvoice:
    invoice: Invoice
    balance: Decimal
    days_past_due: int  # (as_of - due_date).days; zero or less when not due yet
    bucket: str


def aged_open_items(as_of: date, customer: Customer | None = None) -> tuple[list[AgedInvoice], dict[int, Decimal]]:
    """Every invoice with a nonzero balance on as_of, and each customer's unapplied payments and credit notes as a
    negative amount keyed by customer id. A/R aging and Unpaid invoices by customer both read only this, so their
    numbers always agree."""

    def only(queryset):
        return queryset.filter(customer=customer) if customer is not None else queryset

    invoices = []
    for invoice in only(invoices_with_open_balance(as_of)):
        balance = invoice_balance(invoice, as_of)
        if balance != 0:
            days = (as_of - invoice.due_date).days
            invoices.append(AgedInvoice(invoice, balance, days, bucket_for(days)))
    credits: dict[int, Decimal] = {}
    for payment in only(payments_with_unapplied(as_of)):
        unapplied = payment_unapplied(payment, as_of)
        if unapplied != 0:
            credits[payment.customer_id] = money(credits.get(payment.customer_id, ZERO) - unapplied)
    for note in only(credit_notes_with_unapplied(as_of)):
        unapplied = credit_unapplied(note, as_of)
        if unapplied != 0:
            credits[note.customer_id] = money(credits.get(note.customer_id, ZERO) - unapplied)
    return invoices, credits


def _aging_rows(invoices: list[AgedInvoice], credits: dict[int, Decimal]) -> dict[int, AgingRow]:
    rows: dict[int, AgingRow] = {}
    customers = {a.invoice.customer_id: a.invoice.customer for a in invoices}
    missing = [pk for pk in credits if pk not in customers]
    customers.update(Customer.objects.in_bulk(missing))
    for a in invoices:
        row = rows.setdefault(a.invoice.customer_id, AgingRow(customers[a.invoice.customer_id]))
        setattr(row, a.bucket, money(getattr(row, a.bucket) + a.balance))
    for pk, amount in credits.items():
        row = rows.setdefault(pk, AgingRow(customers[pk]))
        row.unapplied_credits = money(row.unapplied_credits + amount)
    return rows


def _sum_rows(rows) -> AgingRow:
    totals = AgingRow(None)
    for row in rows:
        for name in (*BUCKETS, "unapplied_credits"):
            setattr(totals, name, money(getattr(totals, name) + getattr(row, name)))
    return totals


def _ar_ledger(as_of: date) -> Decimal:
    settings = SalesSettings.load()
    return _account_balance(settings.ar_account, as_of) if settings.ar_account_id else ZERO


def ar_aging(as_of: date, customer: Customer | None = None) -> Aging:
    """With a customer, only that customer's row; the ledger comparison is still for the whole A/R account."""
    rows = _aging_rows(*aged_open_items(as_of, customer))
    ordered = sorted(rows.values(), key=lambda r: r.customer.name.lower())
    totals = _sum_rows(ordered)
    ledger = _ar_ledger(as_of)
    return Aging(as_of, ordered, totals, ledger, money(totals.total - ledger))


@dataclass
class HomeReceivables:
    open_ar: Decimal
    overdue_total: Decimal
    overdue_count: int


def home_receivables(today: date) -> HomeReceivables:
    """The Home tiles: A/R aging's total, and open_invoices(today, overdue_only=True) summed, from one pass over
    open items. Skips the ledger comparison A/R aging does, which Home doesn't show."""
    invoices, credits = aged_open_items(today)
    overdue = [a for a in invoices if a.balance > 0 and a.days_past_due > 0]
    return HomeReceivables(
        open_ar=_sum_rows(_aging_rows(invoices, credits).values()).total,
        overdue_total=sum((a.balance for a in overdue), 0),
        overdue_count=len(overdue),
    )


@dataclass
class UnpaidGroup:
    customer: Customer
    invoices: list[AgedInvoice]
    credits: Decimal  # zero or less
    totals: AgingRow


@dataclass
class UnpaidReport:
    as_of: date
    groups: list[UnpaidGroup]
    totals: AgingRow
    ledger_balance: Decimal
    difference: Decimal


def unpaid_by_customer(as_of: date) -> UnpaidReport:
    """Every customer's unpaid invoices, aged, with their unapplied credits; the totals equal A/R aging's."""
    invoices, credits = aged_open_items(as_of)
    rows = _aging_rows(invoices, credits)
    by_customer: dict[int, list[AgedInvoice]] = {}
    for a in invoices:
        by_customer.setdefault(a.invoice.customer_id, []).append(a)
    groups = []
    for pk, row in rows.items():
        if row.total == 0 and not by_customer.get(pk):
            continue
        listed = sorted(by_customer.get(pk, []), key=lambda a: (a.invoice.due_date, a.invoice.date, a.invoice.number))
        groups.append(UnpaidGroup(row.customer, listed, row.unapplied_credits, row))
    groups.sort(key=lambda g: g.customer.name.lower())
    totals = _sum_rows(rows.values())
    ledger = _ar_ledger(as_of)
    return UnpaidReport(as_of, groups, totals, ledger, money(totals.total - ledger))


@dataclass
class OpenInvoiceRow:
    invoice: Invoice
    balance: Decimal
    days_overdue: int


def open_invoices(
    as_of: date | None = None, customer: Customer | None = None, overdue_only: bool = False,
) -> list[OpenInvoiceRow]:
    today = as_of or timezone.localdate()
    invoices = invoices_with_open_balance(as_of).order_by("due_date", "date", "id")
    if customer is not None:
        invoices = invoices.filter(customer=customer)
    rows = []
    for invoice in invoices:
        balance = invoice_balance(invoice, as_of)
        if balance <= 0:
            continue
        days = (today - invoice.due_date).days
        if overdue_only and days <= 0:
            continue
        rows.append(OpenInvoiceRow(invoice, balance, days))
    return rows


@dataclass
class CustomerBalanceRow:
    customer: Customer
    balance: Decimal


def customer_balances(as_of: date | None = None) -> list[CustomerBalanceRow]:
    totals: dict[int, Decimal] = {}
    customers = {c.pk: c for c in Customer.objects.all()}
    for invoice in invoices_with_open_balance(as_of):
        totals[invoice.customer_id] = totals.get(invoice.customer_id, ZERO) + invoice_balance(invoice, as_of)
    for payment in payments_with_unapplied(as_of):
        totals[payment.customer_id] = totals.get(payment.customer_id, ZERO) - payment_unapplied(payment, as_of)
    for note in credit_notes_with_unapplied(as_of):
        totals[note.customer_id] = totals.get(note.customer_id, ZERO) - credit_unapplied(note, as_of)
    rows = [CustomerBalanceRow(customers[pk], money(total)) for pk, total in totals.items() if money(total) != 0]
    return sorted(rows, key=lambda r: r.customer.name.lower())


@dataclass
class OpenOrderRow:
    order: SalesOrder
    line: object
    ordered: int
    left: int


def open_sales_orders() -> list[OpenOrderRow]:
    rows = []
    orders = SalesOrder.objects.filter(status=OrderStatus.OPEN).select_related("customer").order_by("date", "id")
    for order in orders:
        for line in order.lines.select_related("item"):
            left = remaining(line)
            if left > 0:
                rows.append(OpenOrderRow(order, line, line.quantity, left))
    return rows


@dataclass
class UndepositedReport:
    payments: list[Payment]
    total: Decimal
    ledger_balance: Decimal
    difference: Decimal


def undeposited_funds() -> UndepositedReport:
    payments = list(undeposited_payments())
    total = money(sum((p.amount for p in payments), ZERO))
    settings = SalesSettings.load()
    ledger = _account_balance(settings.undeposited_account, None) if settings.undeposited_account_id else ZERO
    return UndepositedReport(payments, total, ledger, money(total - ledger))


def drop_ship_awaiting_bill() -> list[InvoiceLine]:
    return list(
        InvoiceLine.objects.filter(
            fulfillment=Fulfillment.DROP_SHIP, drop_ship_billed=False, invoice__status=DocStatus.OPEN)
        .select_related("invoice", "invoice__customer", "item")
        .order_by("invoice__date", "invoice_id", "position")
    )

"""Sales reports. They read the documents and applications; AR aging and Undeposited Funds also show the ledger
balance they should equal."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from contacts.models import Customer
from ledger.balances import money

from .balances import _counts, credit_unapplied, invoice_balance, payment_unapplied
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


def ar_aging(as_of: date) -> Aging:
    rows: dict[int, AgingRow] = {}

    def row_for(customer) -> AgingRow:
        return rows.setdefault(customer.pk, AgingRow(customer))

    for invoice in Invoice.objects.select_related("customer"):
        balance = invoice_balance(invoice, as_of)
        if balance != 0:
            row = row_for(invoice.customer)
            name = bucket_for((as_of - invoice.due_date).days)
            setattr(row, name, money(getattr(row, name) + balance))
    for payment in Payment.objects.select_related("customer"):
        unapplied = payment_unapplied(payment, as_of)
        if unapplied != 0:
            row = row_for(payment.customer)
            row.unapplied_credits = money(row.unapplied_credits - unapplied)
    for note in CreditNote.objects.select_related("customer"):
        unapplied = credit_unapplied(note, as_of)
        if unapplied != 0:
            row = row_for(note.customer)
            row.unapplied_credits = money(row.unapplied_credits - unapplied)

    ordered = sorted(rows.values(), key=lambda r: r.customer.name.lower())
    totals = AgingRow(None)
    for row in ordered:
        for name in (*BUCKETS, "unapplied_credits"):
            setattr(totals, name, money(getattr(totals, name) + getattr(row, name)))
    settings = SalesSettings.load()
    ledger = _account_balance(settings.ar_account, as_of) if settings.ar_account_id else ZERO
    return Aging(as_of, ordered, totals, ledger, money(totals.total - ledger))


@dataclass
class OpenInvoiceRow:
    invoice: Invoice
    balance: Decimal
    days_overdue: int


def open_invoices(
    as_of: date | None = None, customer: Customer | None = None, overdue_only: bool = False,
) -> list[OpenInvoiceRow]:
    today = as_of or date.today()
    invoices = Invoice.objects.select_related("customer").order_by("due_date", "date", "id")
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
    from .balances import customer_balance

    rows = []
    for customer in Customer.objects.all():
        balance = customer_balance(customer, as_of)
        if balance != 0:
            rows.append(CustomerBalanceRow(customer, balance))
    return rows


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

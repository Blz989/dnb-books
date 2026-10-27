"""Purchasing reports. They read the documents and applications; A/P aging also shows the ledger balance it
should equal."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from contacts.models import Vendor
from ledger.balances import money
from sales.models import DocStatus, OrderStatus
from sales.reports import BUCKETS, bucket_for

from .balances import (
    bill_balance, bills_with_balances, vendor_credit_unapplied, vendor_credits_with_balances,
    vendor_payment_unapplied, vendor_payments_with_balances,
)
from .checks import ap_ledger_balance
from .models import Bill, BillItemLine, PurchaseOrder, Threshold1099, VendorPayment, VendorPaymentMethod
from .posting.orders import billed_quantity

ZERO = Decimal("0.00")


@dataclass
class APAgingRow:
    vendor: Vendor | None
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
class APAging:
    as_of: date
    rows: list[APAgingRow]
    totals: APAgingRow
    ledger_balance: Decimal
    difference: Decimal


def ap_aging(as_of: date) -> APAging:
    rows: dict[int, APAgingRow] = {}

    def row_for(vendor) -> APAgingRow:
        return rows.setdefault(vendor.pk, APAgingRow(vendor))

    for bill in bills_with_balances():
        balance = bill_balance(bill, as_of)
        if balance != 0:
            row = row_for(bill.vendor)
            name = bucket_for((as_of - bill.due_date).days)
            setattr(row, name, money(getattr(row, name) + balance))
    for payment in vendor_payments_with_balances():
        unapplied = vendor_payment_unapplied(payment, as_of)
        if unapplied != 0:
            row = row_for(payment.vendor)
            row.unapplied_credits = money(row.unapplied_credits - unapplied)
    for credit in vendor_credits_with_balances():
        unapplied = vendor_credit_unapplied(credit, as_of)
        if unapplied != 0:
            row = row_for(credit.vendor)
            row.unapplied_credits = money(row.unapplied_credits - unapplied)

    ordered = sorted(rows.values(), key=lambda r: r.vendor.name.lower())
    totals = APAgingRow(None)
    for row in ordered:
        for name in (*BUCKETS, "unapplied_credits"):
            setattr(totals, name, money(getattr(totals, name) + getattr(row, name)))
    ledger = ap_ledger_balance(as_of) or ZERO
    return APAging(as_of, ordered, totals, ledger, money(totals.total - ledger))


@dataclass
class BillDueRow:
    bill: Bill
    balance: Decimal
    days_overdue: int


def bills_due(through: date, today: date | None = None) -> list[BillDueRow]:
    """Open bills due on or before `through`, soonest first. Overdue bills are included."""
    today = today or timezone.localdate()
    rows = []
    for bill in bills_with_balances().filter(status=DocStatus.OPEN, due_date__lte=through).order_by(
            "due_date", "date", "id"):
        balance = bill_balance(bill)
        if balance > 0:
            rows.append(BillDueRow(bill, balance, (today - bill.due_date).days))
    return rows


@dataclass
class VendorBalanceRow:
    vendor: Vendor
    balance: Decimal


def vendor_balances(as_of: date | None = None) -> list[VendorBalanceRow]:
    totals: dict[int, Decimal] = {}
    vendors = {v.pk: v for v in Vendor.objects.all()}
    for bill in bills_with_balances():
        totals[bill.vendor_id] = totals.get(bill.vendor_id, ZERO) + bill_balance(bill, as_of)
    for payment in vendor_payments_with_balances():
        totals[payment.vendor_id] = totals.get(payment.vendor_id, ZERO) - vendor_payment_unapplied(payment, as_of)
    for credit in vendor_credits_with_balances():
        totals[credit.vendor_id] = totals.get(credit.vendor_id, ZERO) - vendor_credit_unapplied(credit, as_of)
    rows = [VendorBalanceRow(vendors[pk], money(total)) for pk, total in totals.items() if money(total) != 0]
    return sorted(rows, key=lambda r: r.vendor.name.lower())


@dataclass
class OpenPORow:
    order: PurchaseOrder
    line: object
    ordered: int
    billed: int
    left: int


def open_purchase_orders() -> list[OpenPORow]:
    rows = []
    orders = PurchaseOrder.objects.filter(status=OrderStatus.OPEN).select_related("vendor").order_by("date", "id")
    for order in orders:
        for line in order.lines.select_related("item"):
            billed = billed_quantity(line)
            if line.quantity - billed > 0:
                rows.append(OpenPORow(order, line, line.quantity, billed, line.quantity - billed))
    return rows


@dataclass
class Row1099:
    vendor: Vendor
    total: Decimal
    file: bool


@dataclass
class Summary1099:
    year: int
    threshold: Decimal | None
    rows: list[Row1099]


def summary_1099(year: int) -> Summary1099:
    """Non-void check and ACH payments dated in the year, for vendors flagged 1099. Card payments are left out:
    the card processor reports them on Form 1099-K."""
    threshold = Threshold1099.objects.filter(year=year).values_list("amount", flat=True).first()
    totals = (
        VendorPayment.objects.filter(
            vendor__is_1099=True, status=DocStatus.OPEN, date__year=year,
            method__in=(VendorPaymentMethod.CHECK, VendorPaymentMethod.ACH))
        .values("vendor").annotate(total=Sum("amount"))
    )
    vendors = {v.pk: v for v in Vendor.objects.filter(is_1099=True)}
    rows = [
        Row1099(vendors[t["vendor"]], money(t["total"]), threshold is not None and money(t["total"]) >= threshold)
        for t in totals if money(t["total"]) != 0
    ]
    return Summary1099(year, threshold, sorted(rows, key=lambda r: r.vendor.name.lower()))

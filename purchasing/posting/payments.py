"""Vendor payments, Pay bills batches, and the applications that tie payments to bills."""
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction

from contacts.models import Vendor
from ledger.models import Account, AccountSubtype
from ledger.posting import LineSpec, post_entry
from sales.models import DocStatus, DocType
from sales.numbering import next_number

from ..balances import bill_balance, vendor_payment_unapplied
from ..errors import PurchasingError
from ..models import (
    Bill, BillPaymentApplication, PurchasingSettings, VendorCreditApplication, VendorPayment, VendorPaymentMethod,
)
from .common import begin_void, check_amount, check_not_before, check_open, finish_void, money, void_date

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class PayLine:
    bill: Bill
    amount: Decimal


def _check_bill(bill: Bill, vendor: Vendor) -> Bill:
    bill = Bill.objects.get(pk=bill.pk)
    if bill.vendor_id != vendor.pk:
        raise PurchasingError(f"{bill.number} belongs to a different vendor.")
    if bill.status == DocStatus.VOID:
        raise PurchasingError(f"{bill.number} is void.")
    return bill


def _add_application(payment: VendorPayment, bill: Bill, amount: Decimal, date: date_type) -> BillPaymentApplication:
    check_amount(amount)
    bill = _check_bill(bill, payment.vendor)
    check_not_before(date, payment, bill)
    unapplied = vendor_payment_unapplied(payment)
    if amount > unapplied:
        raise PurchasingError(f"This payment has {money(unapplied)} left to apply.")
    balance = bill_balance(bill)
    if amount > balance:
        raise PurchasingError(f"{bill.number} has {money(balance)} left to pay.")
    return BillPaymentApplication.objects.create(payment=payment, bill=bill, amount=amount, applied_on=date)


def _normalize_check(number: str) -> str:
    return " ".join((number or "").split()).casefold()


def _check_number_free(paid_from: Account, number: str) -> None:
    wanted = _normalize_check(number)
    used = VendorPayment.objects.filter(
        paid_from=paid_from, method=VendorPaymentMethod.CHECK, status=DocStatus.OPEN)
    if any(_normalize_check(n) == wanted for n in used.values_list("check_number", flat=True)):
        raise PurchasingError(f"Check {number.strip()} is already used on {paid_from}.")


def pay_vendor(
    *, vendor: Vendor, date: date_type, method: VendorPaymentMethod, amount: Decimal, paid_from: Account, user,
    check_number: str = "", reference: str = "", applications: Sequence[tuple[Bill, Decimal]] = (),
) -> VendorPayment:
    """Dr A/P, Cr the bank or card the payment came from. Anything above the applied bills stays unapplied."""
    with transaction.atomic():
        settings = PurchasingSettings.load().require_configured()
        check_amount(amount)
        if method not in VendorPaymentMethod.values:
            raise PurchasingError("Choose check, ACH, or company card.")
        if method == VendorPaymentMethod.CARD:
            if paid_from is None or paid_from.subtype != AccountSubtype.CREDIT_CARD:
                raise PurchasingError("Choose the credit card used.")
        elif paid_from is None or paid_from.subtype != AccountSubtype.BANK:
            raise PurchasingError("Choose the bank account to pay from.")
        check_number = (check_number or "").strip()
        if method == VendorPaymentMethod.CHECK:
            if not check_number:
                raise PurchasingError(f"Enter the check number for {vendor.name}.")
            _check_number_free(paid_from, check_number)
        else:
            check_number = ""
        number = next_number(DocType.VP)
        entry = post_entry(
            date=date, user=user, memo=f"Payment {number} {vendor.name}", reference=check_number or reference,
            source_type="purchasing.payment", source_id=number,
            lines=[LineSpec(settings.ap_account, debit=amount), LineSpec(paid_from, credit=amount)],
        )
        payment = VendorPayment.objects.create(
            number=number, vendor=vendor, date=date, method=method, paid_from=paid_from, check_number=check_number,
            reference=reference, amount=amount, journal_entry=entry, created_by=user)
        for bill, applied in applications:
            _add_application(payment, bill, applied, date)
        return payment


def pay_bills(
    *, date: date_type, method: VendorPaymentMethod, paid_from: Account, lines: Sequence[PayLine], user,
    check_numbers: dict[int, str] | None = None,
) -> list[VendorPayment]:
    """One payment per vendor, ordered by vendor name, all or nothing. `check_numbers` is keyed by vendor id."""
    lines = [l for l in lines if l.amount != 0]
    if not lines:
        raise PurchasingError("Choose at least one bill to pay.")
    check_numbers = check_numbers or {}
    by_vendor: dict[int, list[PayLine]] = defaultdict(list)
    vendors: dict[int, Vendor] = {}
    for line in lines:
        vendors[line.bill.vendor_id] = line.bill.vendor
        by_vendor[line.bill.vendor_id].append(line)
    with transaction.atomic():
        payments = []
        for vendor_id in sorted(vendors, key=lambda v: vendors[v].name.casefold()):
            vendor_lines = by_vendor[vendor_id]
            for line in vendor_lines:
                check_amount(line.amount)
            payments.append(pay_vendor(
                vendor=vendors[vendor_id], date=date, method=method,
                amount=sum((l.amount for l in vendor_lines), ZERO), paid_from=paid_from, user=user,
                check_number=check_numbers.get(vendor_id, ""),
                applications=[(l.bill, l.amount) for l in vendor_lines]))
        return payments


def apply_vendor_payment(
    payment: VendorPayment, bill: Bill, amount: Decimal, *, date: date_type, user,
) -> BillPaymentApplication:
    with transaction.atomic():
        current = VendorPayment.objects.select_related("vendor").get(pk=payment.pk)
        check_open(current)
        return _add_application(current, bill, amount, date)


def remove_bill_application(
    app: BillPaymentApplication | VendorCreditApplication, *, date: date_type, user,
) -> None:
    with transaction.atomic():
        current = type(app).objects.get(pk=app.pk)
        if current.removed:
            raise PurchasingError("That application was already removed.")
        if date < current.applied_on:
            raise PurchasingError("The date can't be before the application date.")
        current.removed = True
        current.removed_on = date
        current.save()


def void_vendor_payment(
    payment: VendorPayment, *, user, reason: str, date: date_type | None = None,
) -> VendorPayment:
    with transaction.atomic():
        current, reason = begin_void(VendorPayment, payment, user, reason)
        active = list(current.applications.filter(removed=False))
        when = void_date(current, date, list(current.applications.all()))
        finish_void(current, user=user, reason=reason, when=when)
        for app in active:
            app.removed, app.removed_on = True, when
            app.save()
    payment.refresh_from_db()
    return payment

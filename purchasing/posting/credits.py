"""Vendor credits: returns of stock and allowances, and applying them to bills."""
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from contacts.models import Vendor
from items.models import InventorySettings, Item, MovementKind
from items.stock import StockLine, current_unit_cost, record_movements, void_movement
from ledger.models import Account, AccountSubtype
from ledger.posting import LineSpec, post_entry
from sales.models import DocType
from sales.numbering import next_number

from ..balances import bill_balance, vendor_credit_unapplied
from ..errors import PurchasingError
from ..models import (
    Bill, PurchasingSettings, VendorCredit, VendorCreditApplication, VendorCreditKind, VendorCreditLine,
)
from .common import begin_void, check_amount, check_not_before, check_open, finish_void, money, void_date
from .payments import _check_bill

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class ReturnLineSpec:
    item: Item
    quantity: int
    amount: Decimal | None = None  # None: quantity x the item's current unit cost


@dataclass(frozen=True)
class AllowanceLineSpec:
    account: Account | None
    amount: Decimal
    description: str = ""


def create_return_credit(
    *, vendor: Vendor, date: date_type, lines: Sequence[ReturnLineSpec], reason: str, user,
    allow_negative: bool = False,
) -> VendorCredit:
    """Stock leaves at engine cost (Dr COGS, Cr Inventory); A/P goes down by the credit amount against COGS, so the
    difference between the two lands in COGS."""
    with transaction.atomic():
        settings = PurchasingSettings.load().require_configured()
        inv = InventorySettings.load()
        if not lines:
            raise PurchasingError("Add at least one line.")
        resolved = []
        for n, line in enumerate(lines, 1):
            if not line.item.is_stock or not line.item.is_active:
                raise PurchasingError(f"Line {n}: {line.item.sku} is not an active stock item.")
            if line.quantity is None or line.quantity <= 0:
                raise PurchasingError(f"Line {n}: enter a quantity above zero.")
            amount = line.amount
            if amount is None:
                amount = (line.quantity * current_unit_cost(line.item)).quantize(CENT, rounding=ROUND_HALF_UP)
            check_amount(amount, f"Line {n}: enter an amount above zero.")
            account = line.item.cogs_account or inv.default_cogs_account
            if account is None:
                raise PurchasingError(f"Line {n}: choose an account for {line.item.sku}.")
            resolved.append((line, amount, account))

        number = next_number(DocType.VC)
        movements = record_movements(
            kind=MovementKind.RECEIPT, date=date, user=user, allow_negative=allow_negative,
            source_type="purchasing.vendor_credit", source_id=number,
            lines=[StockLine(line.item, -line.quantity, account=account) for line, _, account in resolved])
        credits: dict[Account, Decimal] = defaultdict(lambda: ZERO)
        for _, amount, account in resolved:
            credits[account] += amount
        total = sum(credits.values(), ZERO)
        entry = post_entry(
            date=date, user=user, memo=f"Vendor credit {number} {vendor.name}",
            source_type="purchasing.vendor_credit", source_id=number,
            lines=[LineSpec(settings.ap_account, debit=total)]
            + [LineSpec(account, credit=amount) for account, amount in credits.items()])
        credit = VendorCredit.objects.create(
            number=number, vendor=vendor, date=date, kind=VendorCreditKind.RETURN, reason=reason,
            journal_entry=entry, created_by=user)
        for (line, amount, _), movement in zip(resolved, movements):
            VendorCreditLine.objects.create(
                credit=credit, item=line.item, quantity=line.quantity, amount=amount, stock_movement=movement)
        return credit


def create_allowance_credit(
    *, vendor: Vendor, date: date_type, lines: Sequence[AllowanceLineSpec], reason: str, user,
) -> VendorCredit:
    with transaction.atomic():
        settings = PurchasingSettings.load().require_configured()
        if not lines:
            raise PurchasingError("Add at least one line.")
        resolved = []
        for n, line in enumerate(lines, 1):
            check_amount(line.amount, f"Line {n}: enter an amount above zero.")
            account = line.account or vendor.default_expense_account
            if account is None:
                raise PurchasingError(f"Line {n}: choose an account.")
            if account.subtype in (AccountSubtype.INVENTORY, AccountSubtype.ACCOUNTS_PAYABLE):
                raise PurchasingError(f"Line {n}: choose an account other than inventory or accounts payable.")
            resolved.append((line, account))
        total = sum((l.amount for l, _ in resolved), ZERO)
        number = next_number(DocType.VC)
        entry = post_entry(
            date=date, user=user, memo=f"Vendor credit {number} {vendor.name}",
            source_type="purchasing.vendor_credit", source_id=number,
            lines=[LineSpec(settings.ap_account, debit=total)]
            + [LineSpec(account, credit=l.amount, description=l.description) for l, account in resolved])
        credit = VendorCredit.objects.create(
            number=number, vendor=vendor, date=date, kind=VendorCreditKind.ALLOWANCE, reason=reason,
            journal_entry=entry, created_by=user)
        for line, account in resolved:
            VendorCreditLine.objects.create(
                credit=credit, account=account, description=line.description, amount=line.amount)
        return credit


def apply_vendor_credit(
    credit: VendorCredit, bill: Bill, amount: Decimal, *, date: date_type, user,
) -> VendorCreditApplication:
    with transaction.atomic():
        current = VendorCredit.objects.select_related("vendor").get(pk=credit.pk)
        check_open(current)
        check_amount(amount)
        bill = _check_bill(bill, current.vendor)
        check_not_before(date, current, bill)
        unapplied = vendor_credit_unapplied(current)
        if amount > unapplied:
            raise PurchasingError(f"This credit has {money(unapplied)} left to apply.")
        balance = bill_balance(bill)
        if amount > balance:
            raise PurchasingError(f"{bill.number} has {money(balance)} left to pay.")
        return VendorCreditApplication.objects.create(credit=current, bill=bill, amount=amount, applied_on=date)


def void_vendor_credit(
    credit: VendorCredit, *, user, reason: str, date: date_type | None = None, allow_negative: bool = False,
) -> VendorCredit:
    with transaction.atomic():
        current, reason = begin_void(VendorCredit, credit, user, reason)
        if current.applications.filter(removed=False).exists():
            raise PurchasingError(f"{current.number} is applied to bills. Remove the applications first.")
        when = void_date(current, date, list(current.applications.all()))
        for line in current.lines.select_related("stock_movement"):
            if line.stock_movement_id:
                void_movement(line.stock_movement, user=user, reason=reason, date=when, system=True,
                              allow_negative=allow_negative)
        finish_void(current, user=user, reason=reason, when=when)
    credit.refresh_from_db()
    return credit

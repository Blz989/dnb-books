"""Bills: stock received at the bill price, drop-ship cost, and expenses, all against A/P."""
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from core.roles import require_admin
from contacts.models import Terms, Vendor, due_date as terms_due_date
from items.errors import SettingsMissingError
from items.models import InventorySettings, Item, MovementKind
from items.stock import StockLine, record_movements, void_movement
from ledger.locks import lock_message
from ledger.models import Account, AccountSubtype
from ledger.periods import assert_open
from ledger.posting import LineSpec, post_entry, void_entry
from sales.models import DocStatus, DocType, OrderStatus
from sales.numbering import next_number

from .. import dropship
from ..balances import bill_applied
from ..errors import MAX_AMOUNT_TEXT, MAX_PRICE_TEXT, MAX_QUANTITY, PurchasingError
from ..models import (
    Bill, BillExpenseLine, BillItemLine, BillLineKind, BillRevision, PurchaseOrder, PurchaseOrderLine,
    PurchasingSettings,
)
from .orders import po_remaining, refresh_po_status

CENT = Decimal("0.01")


@dataclass(frozen=True)
class BillItemSpec:
    item: Item
    quantity: int
    price: Decimal
    description: str = ""
    po_line: PurchaseOrderLine | None = None


@dataclass(frozen=True)
class BillExpenseSpec:
    account: Account
    amount: Decimal
    description: str = ""


class DuplicateBillWarning(PurchasingError):
    """Raised before saving when the vendor already has a bill with this invoice number. Callers confirm and
    retry with allow_duplicate=True."""


def line_amount(spec: BillItemSpec) -> Decimal:
    return (spec.quantity * spec.price).quantize(CENT, rounding=ROUND_HALF_UP)


def line_kind(spec: BillItemSpec) -> BillLineKind:
    if spec.po_line is not None and spec.po_line.so_line_id:
        return BillLineKind.DROP_SHIP
    return BillLineKind.STOCK if spec.item.is_stock else BillLineKind.NON_STOCK


def normalize_invoice_number(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def find_duplicate(vendor: Vendor, vendor_invoice_number: str, exclude_bill: Bill | None = None) -> Bill | None:
    wanted = normalize_invoice_number(vendor_invoice_number)
    if not wanted:
        return None
    others = Bill.objects.filter(vendor=vendor, status="OPEN")
    if exclude_bill is not None:
        others = others.exclude(pk=exclude_bill.pk)
    for bill in others.order_by("id"):
        if normalize_invoice_number(bill.vendor_invoice_number) == wanted:
            return bill
    return None


def _debit_account(spec: BillItemSpec, kind: BillLineKind, vendor: Vendor, n: int, inv: InventorySettings) -> Account:
    if kind == BillLineKind.DROP_SHIP:
        account = spec.item.cogs_account or inv.default_cogs_account
    else:
        account = vendor.default_expense_account or inv.default_cogs_account
    if account is None:
        raise PurchasingError(f"Line {n}: choose an account for {spec.item.sku}.")
    return account


def validate_bill(
    *, vendor: Vendor, date: date_type, item_lines: Sequence[BillItemSpec], expense_lines: Sequence[BillExpenseSpec],
    purchase_order: PurchaseOrder | None, vendor_invoice_number: str, exclude_bill: Bill | None = None,
    allow_duplicate: bool = False, check_vendor: bool = True,
) -> None:
    """Every check that can fail before anything is saved."""
    PurchasingSettings.load().require_configured()
    if check_vendor and not vendor.is_active:
        raise PurchasingError(f"{vendor.name} is inactive.")
    if not item_lines and not expense_lines:
        raise PurchasingError("A bill needs at least one line.")
    inv = InventorySettings.load()
    for n, spec in enumerate(item_lines, 1):
        if not spec.item.is_active:
            raise PurchasingError(f"Line {n}: {spec.item.sku} is not an active item.")
        if spec.quantity is None or spec.quantity <= 0:
            raise PurchasingError(f"Line {n}: enter a quantity above zero.")
        if spec.quantity > MAX_QUANTITY:
            raise PurchasingError(f"Line {n}: that quantity is too large.")
        if not isinstance(spec.price, Decimal):
            raise TypeError("Prices must be Decimal, never float or int.")
        if not spec.price.is_finite():
            raise PurchasingError(f"Line {n}: enter a price like 12.50.")
        if spec.price < 0:
            raise PurchasingError(f"Line {n}: price can't be negative.")
        if spec.price >= Decimal(MAX_PRICE_TEXT) or line_amount(spec) > Decimal(MAX_AMOUNT_TEXT):
            raise PurchasingError(f"Line {n}: that amount is too large.")
        if spec.price.as_tuple().exponent < -4:
            raise PurchasingError(f"Line {n}: price can have at most 4 decimal places.")
    for n, spec in enumerate(expense_lines, len(item_lines) + 1):
        if not isinstance(spec.amount, Decimal):
            raise TypeError("Amounts must be Decimal, never float or int.")
        if not spec.amount.is_finite() or spec.amount <= 0:
            raise PurchasingError(f"Line {n}: enter an amount above zero.")
        if spec.amount > Decimal(MAX_AMOUNT_TEXT):
            raise PurchasingError(f"Line {n}: that amount is too large.")
        if spec.amount.as_tuple().exponent < -2:
            raise PurchasingError(f"Line {n}: amounts can have at most 2 decimal places.")
        if spec.account.subtype in (AccountSubtype.INVENTORY, AccountSubtype.ACCOUNTS_PAYABLE):
            raise PurchasingError(f"Line {n}: choose an account other than inventory or accounts payable.")
    assert_open(date)

    if purchase_order is not None:
        if purchase_order.vendor_id != vendor.pk:
            raise PurchasingError(f"{purchase_order.number} is for a different vendor.")
        if purchase_order.status == OrderStatus.CLOSED and exclude_bill is None:
            raise PurchasingError(f"{purchase_order.number} is closed. Reopen it to bill it.")
    wanted: dict[int, int] = defaultdict(int)
    for n, spec in enumerate(item_lines, 1):
        if spec.po_line is None:
            continue
        if purchase_order is None or spec.po_line.order_id != purchase_order.pk:
            raise PurchasingError(f"Line {n}: that line isn't on the purchase order.")
        wanted[spec.po_line.pk] += spec.quantity
        left = po_remaining(spec.po_line, exclude_bill=exclude_bill) - (wanted[spec.po_line.pk] - spec.quantity)
        if spec.quantity > left:
            raise PurchasingError(f"Line {n}: only {max(left, 0)} left to receive on {purchase_order.number}.")

    if any(line_kind(s) == BillLineKind.STOCK for s in item_lines) and not inv.is_configured:
        raise SettingsMissingError("Set up inventory accounts first.")
    for n, spec in enumerate(item_lines, 1):
        kind = line_kind(spec)
        if kind != BillLineKind.STOCK:
            _debit_account(spec, kind, vendor, n, inv)

    if not allow_duplicate:
        duplicate = find_duplicate(vendor, vendor_invoice_number, exclude_bill)
        if duplicate is not None:
            raise DuplicateBillWarning(
                f"{vendor.name} already has bill {duplicate.number} with invoice # "
                f"{duplicate.vendor_invoice_number}. Save anyway?")


def post_lines(
    bill: Bill, item_lines: Sequence[BillItemSpec], expense_lines: Sequence[BillExpenseSpec], *, user,
    allow_negative: bool,
) -> None:
    """Create the bill's lines, receive its stock, and post the rest. Runs inside the caller's transaction."""
    settings = PurchasingSettings.load()
    inv = InventorySettings.load()
    kinds = [line_kind(s) for s in item_lines]
    amounts = [line_amount(s) for s in item_lines]

    stock_positions = [n for n, kind in enumerate(kinds) if kind == BillLineKind.STOCK]
    movements = []
    if stock_positions:
        movements = record_movements(
            kind=MovementKind.RECEIPT, date=bill.date, user=user, allow_negative=allow_negative,
            source_type="purchasing.bill", source_id=bill.number,
            lines=[StockLine(item_lines[n].item, item_lines[n].quantity, unit_cost=item_lines[n].price,
                             account=settings.ap_account) for n in stock_positions],
        )
    movement_for = dict(zip(stock_positions, movements))

    debits: dict[Account, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for n, (spec, kind, amount) in enumerate(zip(item_lines, kinds, amounts)):
        BillItemLine.objects.create(
            bill=bill, item=spec.item, description=spec.description, quantity=spec.quantity, price=spec.price,
            amount=amount, po_line=spec.po_line, kind=kind, stock_movement=movement_for.get(n), position=n)
        if kind != BillLineKind.STOCK:
            debits[_debit_account(spec, kind, bill.vendor, n + 1, inv)] += amount
    for n, spec in enumerate(expense_lines):
        BillExpenseLine.objects.create(
            bill=bill, account=spec.account, description=spec.description, amount=spec.amount,
            position=len(item_lines) + n)
        debits[spec.account] += spec.amount

    total = sum(debits.values(), Decimal("0.00"))
    entry = None
    if total != 0:
        entry = post_entry(
            date=bill.date, user=user, memo=f"Bill {bill.number} {bill.vendor.name}",
            source_type="purchasing.bill", source_id=bill.number,
            lines=[LineSpec(account, debit=amount) for account, amount in debits.items() if amount != 0]
            + [LineSpec(settings.ap_account, credit=total)],
        )
    bill.journal_entry = entry
    bill.save()


def _drop_ship_so_lines(item_lines: Sequence[BillItemSpec]) -> set[int]:
    return {s.po_line.so_line_id for s in item_lines if line_kind(s) == BillLineKind.DROP_SHIP}


def create_bill(
    *, vendor: Vendor, date: date_type, vendor_invoice_number: str, item_lines: Sequence[BillItemSpec] = (),
    expense_lines: Sequence[BillExpenseSpec] = (), user, purchase_order: PurchaseOrder | None = None,
    terms: Terms | None = None, notes: str = "", allow_duplicate: bool = False, allow_negative: bool = False,
) -> Bill:
    if purchase_order is None:
        po_ids = {s.po_line.order for s in item_lines if s.po_line is not None}
        if len(po_ids) == 1:
            purchase_order = po_ids.pop()
    with transaction.atomic():
        validate_bill(
            vendor=vendor, date=date, item_lines=item_lines, expense_lines=expense_lines,
            purchase_order=purchase_order, vendor_invoice_number=vendor_invoice_number,
            allow_duplicate=allow_duplicate)
        terms = terms or vendor.terms
        bill = Bill.objects.create(
            number=next_number(DocType.BILL), vendor=vendor, vendor_invoice_number=vendor_invoice_number.strip(),
            date=date, terms=terms, due_date=terms_due_date(terms, date), notes=notes,
            purchase_order=purchase_order, created_by=user)
        post_lines(bill, item_lines, expense_lines, user=user, allow_negative=allow_negative)
        dropship.rebuild(_drop_ship_so_lines(item_lines))
        if purchase_order is not None:
            refresh_po_status(purchase_order)
        return bill


def bill_from_po(
    po: PurchaseOrder, *, date: date_type, vendor_invoice_number: str, quantities: dict[int, int],
    prices: dict[int, Decimal] | None = None, expense_lines: Sequence[BillExpenseSpec] = (), user,
    allow_duplicate: bool = False, terms: Terms | None = None, notes: str = "", allow_negative: bool = False,
) -> Bill:
    """Bill part or all of a purchase order. `quantities` and `prices` are keyed by PO line id."""
    by_id = {l.pk: l for l in po.lines.select_related("item")}
    unknown = set(quantities) - set(by_id)
    if unknown:
        raise PurchasingError(f"A line doesn't belong to {po.number}.")
    prices = prices or {}
    specs = [
        BillItemSpec(line.item, quantities[line.pk], prices.get(line.pk, line.price),
                     description=line.description, po_line=line)
        for line in by_id.values() if quantities.get(line.pk, 0) != 0
    ]
    return create_bill(
        vendor=po.vendor, date=date, vendor_invoice_number=vendor_invoice_number, item_lines=specs,
        expense_lines=expense_lines, user=user, purchase_order=po, terms=terms, notes=notes,
        allow_duplicate=allow_duplicate, allow_negative=allow_negative)


def _snapshot(bill: Bill) -> dict:
    return {
        "vendor_id": bill.vendor_id, "vendor_invoice_number": bill.vendor_invoice_number,
        "date": bill.date.isoformat(), "terms": bill.terms, "due_date": bill.due_date.isoformat(),
        "notes": bill.notes,
        "item_lines": [
            {
                "item_id": l.item_id, "sku": l.item.sku, "description": l.description, "quantity": l.quantity,
                "price": str(l.price), "amount": str(l.amount), "kind": l.kind, "po_line_id": l.po_line_id,
            }
            for l in bill.item_lines.select_related("item")
        ],
        "expense_lines": [
            {
                "account_id": l.account_id, "account": l.account.number, "description": l.description,
                "amount": str(l.amount),
            }
            for l in bill.expense_lines.select_related("account")
        ],
    }


def _active_applications(bill: Bill):
    return [*bill.payment_applications.filter(removed=False), *bill.credit_applications.filter(removed=False)]


def _old_drop_ship_so_lines(bill: Bill) -> set[int]:
    return {
        l.po_line.so_line_id for l in bill.item_lines.select_related("po_line")
        if l.kind == BillLineKind.DROP_SHIP and l.po_line is not None
    }


def _reverse_posting(entry, movements, *, user, when: date_type, reason: str, allow_negative: bool) -> None:
    """Reverse a bill's entry and stock receipts as of `when`. These are system voids: a Standard user's edit
    does them on their behalf, and no view can ask for system=True."""
    if entry is not None:
        void_entry(entry, user=user, reason=reason, date=when, system=True)
    for movement in movements:
        void_movement(movement, user=user, reason=reason, date=when, system=True, allow_negative=allow_negative)


def edit_bill(
    bill: Bill, *, user, date: date_type, vendor_invoice_number: str, item_lines: Sequence[BillItemSpec],
    expense_lines: Sequence[BillExpenseSpec], terms: Terms | None = None, notes: str | None = None,
    vendor: Vendor | None = None, reason: str = "", allow_duplicate: bool = False, allow_negative: bool = False,
) -> Bill:
    with transaction.atomic():
        current = Bill.objects.select_related("vendor", "purchase_order", "journal_entry").get(pk=bill.pk)
        if current.status == DocStatus.VOID:
            raise PurchasingError(f"{current.number} is void and can't be edited.")
        new_vendor = vendor or current.vendor
        changed_vendor = new_vendor.pk != current.vendor_id
        applications = _active_applications(current)
        for application in current.payment_applications.filter(removed=False).select_related("payment__journal_entry"):
            entry = application.payment.journal_entry
            locked = lock_message(entry) if entry is not None else None
            if locked:
                raise PurchasingError(locked)
        if changed_vendor and applications:
            raise PurchasingError("The vendor can't change after payments or credits are applied.")
        assert_open(current.date)
        first_applied = min(
            [a.applied_on for a in current.payment_applications.all()]
            + [a.applied_on for a in current.credit_applications.all()], default=None)
        if first_applied is not None and date > first_applied:
            raise PurchasingError(
                f"The bill date can't be after {first_applied.isoformat()}, when a payment or credit was applied.")
        validate_bill(
            vendor=new_vendor, date=date, item_lines=item_lines, expense_lines=expense_lines,
            purchase_order=current.purchase_order, vendor_invoice_number=vendor_invoice_number,
            exclude_bill=current, allow_duplicate=allow_duplicate, check_vendor=changed_vendor)
        new_total = (
            sum((line_amount(s) for s in item_lines), Decimal("0.00"))
            + sum((s.amount for s in expense_lines), Decimal("0.00")))
        applied = bill_applied(current)
        if new_total < applied:
            raise PurchasingError(f"This bill has {applied:,.2f} applied. The new total can't be less than that.")

        BillRevision.objects.create(
            bill=current, revision=current.revision, snapshot=_snapshot(current), edited_by=user, reason=reason)
        old_date, old_entry = current.date, current.journal_entry
        old_movements = [l.stock_movement for l in current.item_lines.all() if l.stock_movement_id]
        old_so_lines = _old_drop_ship_so_lines(current)
        current.item_lines.all().delete()
        current.expense_lines.all().delete()

        terms = terms or current.terms
        current.vendor = new_vendor
        current.vendor_invoice_number = vendor_invoice_number.strip()
        current.date = date
        current.terms = terms
        current.due_date = terms_due_date(terms, date)
        if notes is not None:
            current.notes = notes
        current.revision += 1
        current.journal_entry = None
        current.save()
        # The new receipts go in before the old ones come out, so stock only counts as "going negative" when the
        # edit really leaves less on hand than has been sold.
        post_lines(current, item_lines, expense_lines, user=user, allow_negative=allow_negative)
        _reverse_posting(
            old_entry, old_movements, user=user, when=old_date, reason=f"Edited to revision {current.revision}",
            allow_negative=allow_negative)
        dropship.rebuild(old_so_lines | _drop_ship_so_lines(item_lines))
        if current.purchase_order_id:
            refresh_po_status(current.purchase_order)
    bill.refresh_from_db()
    return bill


def void_bill(
    bill: Bill, *, user, reason: str, date: date_type | None = None, allow_negative: bool = False,
) -> Bill:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise PurchasingError("Enter a reason.")
    with transaction.atomic():
        current = Bill.objects.select_related("journal_entry", "purchase_order").get(pk=bill.pk)
        if current.status == DocStatus.VOID:
            raise PurchasingError(f"{current.number} is already void.")
        if _active_applications(current):
            raise PurchasingError(f"{current.number} has payments or credits applied. Remove them first.")
        removed = [a.removed_on for a in [*current.payment_applications.all(), *current.credit_applications.all()]
                   if a.removed_on]
        when = date or max([current.date, *removed])
        if when < current.date:
            raise PurchasingError("The void date can't be before the bill date.")
        if removed and when < max(removed):
            raise PurchasingError(
                f"The void date can't be before {max(removed).isoformat()}, when a payment or credit was removed.")
        assert_open(when)
        movements = [l.stock_movement for l in current.item_lines.all() if l.stock_movement_id]
        so_lines = _old_drop_ship_so_lines(current)
        _reverse_posting(
            current.journal_entry, movements, user=user, when=when, reason=reason, allow_negative=allow_negative)
        current.status = DocStatus.VOID
        current.voided_on = when
        current.void_reason = reason
        current.save()
        dropship.rebuild(so_lines)
        if current.purchase_order_id:
            refresh_po_status(current.purchase_order)
    bill.refresh_from_db()
    return bill

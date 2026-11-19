"""The Adjustment on bills and invoices for the write tools. On an edit, a left-out adjustment keeps the current
one and an amount of 0.00 removes it."""
from purchasing.models import PurchasingSettings
from purchasing.posting.bills import BillAdjustmentSpec, bill_adjustment
from sales.posting.invoices import InvoiceAdjustmentSpec, invoice_adjustment

from . import lookup
from .runtime import Refused, money

KEEP = object()


def _note(given: dict, current) -> str:
    note = (given.get("note") or "").strip()
    return note or (current.description if current is not None else "") or "Adjustment"


def bill_spec(given: dict | None, bill=None) -> BillAdjustmentSpec | None:
    current = bill_adjustment(bill) if bill is not None else None
    if given is None:
        if current is None:
            return None
        return BillAdjustmentSpec(current.account, current.amount, current.description, current.spreads_to_orders)
    amount = money(given.get("amount"), "adjustment amount")
    if amount == 0:
        return None
    if (given.get("account") or "").strip():
        account = lookup.account(given["account"])
    elif current is not None:
        account = current.account
    else:
        account = PurchasingSettings.load().default_adjustment_account
        if account is None:
            raise Refused("Choose the adjustment account. No default adjustment account is set in Settings, Purchases.")
    return BillAdjustmentSpec(account, amount, _note(given, current))


def invoice_spec(given: dict | None, invoice=None) -> InvoiceAdjustmentSpec | None:
    current = invoice_adjustment(invoice) if invoice is not None else None
    if given is None:
        if current is None:
            return None
        return InvoiceAdjustmentSpec(current.amount, current.description, current.item)
    amount = money(given.get("amount"), "adjustment amount")
    if amount == 0:
        return None
    return InvoiceAdjustmentSpec(amount, _note(given, current))

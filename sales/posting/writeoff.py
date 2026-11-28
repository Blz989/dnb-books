"""Bad debt write-off (spec 5): a credit note to the bad debt account, applied to the invoice in one step."""
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction

from core.roles import require_admin

from ..errors import SalesError
from ..models import DocStatus, Invoice, SalesSettings
from .receipts import CreditLineSpec, apply_credit, create_credit_note


class WriteOffError(SalesError):
    pass


def write_off(invoice: Invoice, *, date: date_type, amount: Decimal, reason: str, user):
    require_admin(user)
    reason = (reason or "").strip()
    with transaction.atomic():
        current = Invoice.objects.select_related("customer").get(pk=invoice.pk)
        account = SalesSettings.load().bad_debt_account
        if account is None:
            raise WriteOffError("Set the bad debt account in Settings, Sales first.")
        if current.status == DocStatus.VOID:
            raise WriteOffError(f"{current.number} is void and can't be written off.")
        if not reason:
            raise WriteOffError("Enter a reason.")
        if amount is None or amount <= 0:
            raise WriteOffError("Enter an amount above zero.")
        if date < current.date:
            raise WriteOffError("The write-off date can't be before the invoice date.")
        note = create_credit_note(
            customer=current.customer, date=date, user=user, reason=f"Bad debt: {reason}"[:200],
            lines=[CreditLineSpec(f"Bad debt write-off of {current.number}", amount, account)])
        note.write_off_of = current
        note.save(update_fields=["write_off_of"])
        apply_credit(note, current, amount, date=date, user=user)  # refuses more than the balance
        return note

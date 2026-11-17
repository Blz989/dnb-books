"""Deleting an invoice hides a voided one from the lists. Nothing is posted: the void already reversed the ledger."""
from django.db import transaction
from django.utils import timezone

from core.roles import require_admin

from ..errors import InvoiceError
from ..models import DocStatus, Invoice


def delete_invoice(invoice: Invoice, *, user) -> Invoice:
    require_admin(user)
    with transaction.atomic():
        current = Invoice.objects.get(pk=invoice.pk)
        if current.deleted_at:
            raise InvoiceError(f"{current.number} is already deleted.")
        if current.status != DocStatus.VOID:
            raise InvoiceError("Void this invoice first. Only a voided invoice can be deleted.")
        current.deleted_at = timezone.now()
        current.deleted_by = user
        current.save()
    invoice.refresh_from_db()
    return invoice

"""Files kept with a record (spec 1). Anyone signed in adds; only an admin removes."""
from dataclasses import dataclass

from django.apps import apps
from django.contrib.contenttypes.models import ContentType

from .models import Attachment
from .roles import is_admin

MAX_BYTES = 10 * 1024 * 1024


class AttachmentError(Exception):
    pass


@dataclass(frozen=True)
class Kind:
    model: str  # app_label.ModelName


# kind (used in URLs) -> model. Every page in spec 1.1.
KINDS: dict[str, Kind] = {
    "invoice": Kind("sales.Invoice"),
    "sales-order": Kind("sales.SalesOrder"),
    "credit-note": Kind("sales.CreditNote"),
    "payment": Kind("sales.Payment"),
    "bill": Kind("purchasing.Bill"),
    "purchase-order": Kind("purchasing.PurchaseOrder"),
    "vendor-credit": Kind("purchasing.VendorCredit"),
    "vendor-payment": Kind("purchasing.VendorPayment"),
    "journal-entry": Kind("ledger.JournalEntry"),
    "customer": Kind("contacts.Customer"),
    "vendor": Kind("contacts.Vendor"),
}
_BY_LABEL = {k.model: name for name, k in KINDS.items()}


def kind_of(obj) -> str:
    return _BY_LABEL[obj._meta.label]


def model_for(kind: str):
    spec = KINDS.get(kind)
    return apps.get_model(spec.model) if spec else None


def sniff(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def for_object(obj):
    return Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk).select_related("uploaded_by")


def add(obj, upload, *, user) -> Attachment:
    if upload is None or not upload.size:
        raise AttachmentError("Choose a file to add.")
    if upload.size > MAX_BYTES:
        raise AttachmentError("That file is over 10 MB. Save it smaller and try again.")
    upload.seek(0)
    mime = sniff(upload.read(16))
    upload.seek(0)
    if mime is None:
        raise AttachmentError("Only PDF, PNG, and JPEG files can be added.")
    name = (upload.name or "file").replace("\\", "/").rsplit("/", 1)[-1][:255]
    return Attachment.objects.create(
        content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk, file=upload, name=name,
        size=upload.size, mime=mime, uploaded_by=user)


def remove(attachment: Attachment, *, user) -> None:
    if not is_admin(user):
        raise AttachmentError("Only an admin can remove an attachment.")
    stored = attachment.file
    attachment.delete()
    stored.delete(save=False)

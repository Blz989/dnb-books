"""Upload links: Claude hands the person a link, the person uploads, and the files join the document.

A link made for a preview holds its files (no target) until confirm_change attaches them. After the confirm,
or for a link made for an existing document, files go straight onto the document."""
import secrets
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from purchasing.models import Bill, VendorPayment
from sales.models import Payment

from .models import Attachment, UploadLink

LINK_LIFETIME = timedelta(minutes=60)
TOOL_MODEL = {"create_bill": Bill, "receive_payment": Payment, "pay_bills": VendorPayment}

LINK_OFF = "Upload links are off. An admin can set DNB_APP_URL."
SOMEONE_ELSE = "This upload link belongs to someone else."
EXPIRED = "This upload link expired. Ask Claude for a new one."
PREVIEW_EXPIRED = "That preview expired, so there's nothing to attach to. Ask Claude for it again."


def enabled() -> bool:
    return bool(settings.DNB_APP_URL)


def make_link(user, *, pending_change=None, target=None) -> UploadLink:
    if (pending_change is None) == (target is None):
        raise ValueError("An upload link is for a preview or for a document, not both or neither.")
    now = timezone.now()
    link = UploadLink(token=secrets.token_urlsafe(32), user=user, pending_change=pending_change,
                      created_at=now, expires_at=now + LINK_LIFETIME)
    if target is not None:
        link.target = target
    link.save()
    return link


def url(link: UploadLink) -> str:
    return f"{settings.DNB_APP_URL}/attach/{link.token}/"


def resolve_target(link: UploadLink):
    """The document files go to now, or None while the preview is not confirmed yet (files are held)."""
    if link.target_id is not None:
        return link.target
    change = link.pending_change
    if change is None or not change.document:
        return None
    model = TOOL_MODEL.get(change.tool)
    return model.objects.filter(number=change.document).first() if model else None


def problem(link: UploadLink, user, now=None) -> str:
    """Why this link can't take files right now, or "" when it can."""
    now = now or timezone.now()
    if link.user_id != user.pk:
        return SOMEONE_ELSE
    if now >= link.expires_at:
        return EXPIRED
    change = link.pending_change
    if link.target_id is None and (change is None or (change.used_at is None and now >= change.expires_at)):
        return PREVIEW_EXPIRED
    return ""


def heading(link: UploadLink) -> str:
    target = resolve_target(link)
    if target is not None:
        return f"Attach files to {target.number}"
    data = (link.pending_change.payload or {}).get("input", {}) if link.pending_change else {}
    tool = link.pending_change.tool if link.pending_change else ""
    if tool == "receive_payment":
        return f"Attach files to the payment from {data.get('customer', '')} (preview, not saved yet)"
    if tool == "pay_bills":
        return f"Attach files to the payment to {data.get('vendor', '')} (preview, not saved yet)"
    return f"Attach files to the bill for {data.get('vendor', '')} (preview, not saved yet)"


def attach_held(pending_change, document) -> int:
    """Give every file held on this change's links its document. Runs inside confirm_change's transaction."""
    held = Attachment.objects.filter(upload_link__pending_change=pending_change, target_id__isnull=True,
                                     removed_at__isnull=True)
    count = 0
    for attachment in held:
        attachment.target = document
        attachment.save(update_fields=["target_type", "target_id"])
        count += 1
    return count


def clean_expired(now=None) -> int:
    """Delete files held for previews that expired unconfirmed. They were never attached to anything."""
    now = now or timezone.now()
    held = Attachment.objects.filter(target_id__isnull=True, upload_link__isnull=False, removed_at__isnull=True)
    stale = [a for a in held.select_related("upload_link__pending_change")
             if a.upload_link.pending_change is None
             or (a.upload_link.pending_change.used_at is None and now >= a.upload_link.pending_change.expires_at)]
    removed = 0
    for attachment in stale:
        name = attachment.file.name
        with transaction.atomic():
            attachment.delete()
        if not Attachment.objects.filter(file=name).exists():
            (Path(settings.MEDIA_ROOT) / name).unlink(missing_ok=True)
        removed += 1
    return removed

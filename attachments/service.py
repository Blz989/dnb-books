"""Adding, listing, and removing attachments. Nothing here touches the ledger."""
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import QuerySet
from django.urls import reverse
from django.utils import timezone

from core.roles import is_admin, require_admin
from purchasing.models import Bill, VendorPayment
from sales.models import Payment

from .files import EXT, MAX_BYTES, MAX_PER_DOCUMENT, sniff
from .models import Attachment, Source

TARGETS = (Bill, Payment, VendorPayment)

WRONG_TYPE = "This file type can't be attached. Use PDF, JPEG, or PNG."
TOO_LARGE = "This file is larger than 10 MB. Save a smaller copy and try again."
TOO_MANY = "This document already has 10 files attached."
DUPLICATE = "This file is already attached to {number}."
NEED_REASON = "Enter a reason for removing this file."


class AttachmentError(Exception):
    """A plain-language message shown as is."""


@dataclass
class AddResult:
    added: list[Attachment] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (file name, message)


def _check_target(target) -> None:
    if not isinstance(target, TARGETS):
        raise AttachmentError("Files can only be attached to bills and payments.")


def _for_target(target) -> QuerySet[Attachment]:
    return Attachment.objects.filter(target_type=ContentType.objects.get_for_model(target), target_id=target.pk)


def active(target) -> QuerySet[Attachment]:
    return _for_target(target).filter(removed_at__isnull=True).select_related("uploaded_by")


def removed(target) -> QuerySet[Attachment]:
    return _for_target(target).filter(removed_at__isnull=False).select_related("removed_by", "uploaded_by")


def label(target) -> str:
    return target.number


def document_url(target) -> str:
    if isinstance(target, Bill):
        return reverse("purchasing:bill-detail", args=[target.number])
    if isinstance(target, VendorPayment):
        return reverse("purchasing:payment-detail", args=[target.number])
    return reverse("sales:payment-detail", args=[target.number])


def _store(upload) -> tuple[str, str, int, object]:
    """Stream the upload to a temp file while hashing it. Returns (temp path, sha256, size, kind)."""
    tmp_dir = Path(settings.MEDIA_ROOT) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    digest, size, head = hashlib.sha256(), 0, b""
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="upload-")
    with os.fdopen(fd, "wb") as out:
        for chunk in upload.chunks():
            if len(head) < 8:
                head += chunk[: 8 - len(head)]
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
    return tmp_path, digest.hexdigest(), size, sniff(head)


def _place(tmp_path: str, sha256: str, kind) -> str:
    """Move the temp file to attachments/YYYY/MM/<sha256>.<ext>, or reuse an identical file already stored."""
    existing = Attachment.objects.filter(sha256=sha256).values_list("file", flat=True).first()
    if existing and (Path(settings.MEDIA_ROOT) / existing).exists():
        os.unlink(tmp_path)
        return existing
    now = timezone.localdate()
    name = f"attachments/{now:%Y}/{now:%m}/{sha256}.{EXT[kind]}"
    final = Path(settings.MEDIA_ROOT) / name
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        os.unlink(tmp_path)
    else:
        os.replace(tmp_path, final)
    return name


def _siblings(target, link) -> QuerySet[Attachment]:
    """The files the count and duplicate rules compare against."""
    if target is not None:
        return _for_target(target).filter(removed_at__isnull=True)
    return Attachment.objects.filter(upload_link=link, target_id__isnull=True, removed_at__isnull=True)


def add_files(user, target, files, *, source=Source.APP, link=None) -> AddResult:
    """Check and store each file on its own. `target` is None only for files held on an upload link."""
    if target is None and link is None:
        raise AttachmentError("Choose a document to attach files to.")
    if target is not None:
        _check_target(target)
    result = AddResult()
    for upload in files:
        name = Path(upload.name or "file").name
        if upload.size is not None and upload.size > MAX_BYTES:
            result.refused.append((name, TOO_LARGE))
            continue
        tmp_path, sha256, size, kind = _store(upload)
        problem = None
        siblings = _siblings(target, link)
        if kind is None:
            problem = WRONG_TYPE
        elif size > MAX_BYTES:
            problem = TOO_LARGE
        elif siblings.filter(sha256=sha256).exists():
            problem = DUPLICATE.format(number=label(target)) if target is not None else (
                "This file is already on this upload link.")
        elif siblings.count() >= MAX_PER_DOCUMENT:
            problem = TOO_MANY
        if problem:
            os.unlink(tmp_path)
            result.refused.append((name, problem))
            continue
        stored = _place(tmp_path, sha256, kind)
        attachment = Attachment(
            original_name=name[:255], kind=kind, size_bytes=size, sha256=sha256, source=source, uploaded_by=user)
        attachment.file.name = stored
        if target is not None:
            attachment.target = target
        if link is not None:
            attachment.upload_link = link
        attachment.save()
        result.added.append(attachment)
    return result


def remove(user, attachment: Attachment, reason: str) -> None:
    require_admin(user)
    reason = (reason or "").strip()
    if not reason:
        raise AttachmentError(NEED_REASON)
    attachment.removed_at = timezone.now()
    attachment.removed_by = user
    attachment.removed_reason = reason[:200]
    attachment.save(update_fields=["removed_at", "removed_by", "removed_reason"])


def can_view(user, attachment: Attachment) -> bool:
    if not (getattr(user, "is_authenticated", False) and user.is_active):
        return False
    if attachment.removed_at is not None:
        return is_admin(user)
    if attachment.target_id is None:
        return attachment.uploaded_by_id == user.pk
    return True

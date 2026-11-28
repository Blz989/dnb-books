import os
import uuid

from auditlog.registry import auditlog
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone


def attachment_path(instance, filename: str) -> str:
    """attachments/<yyyy>/<mm>/<random><ext>: the original name is kept on the record, never in the path."""
    ext = os.path.splitext(filename)[1].lower()[:10]
    now = timezone.localtime()
    return f"attachments/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{ext}"


class Attachment(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT, related_name="+")
    object_id = models.PositiveIntegerField()
    record = GenericForeignKey("content_type", "object_id")
    file = models.FileField(upload_to=attachment_path, max_length=200)
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    mime = models.CharField(max_length=50)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at", "id"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return self.name


auditlog.register(Attachment, exclude_fields=["file"])

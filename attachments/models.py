"""Files kept with bills and payments: the vendor's bill or receipt, a check image, a remittance."""
from auditlog.registry import auditlog
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

USER = settings.AUTH_USER_MODEL


class Kind(models.TextChoices):
    PDF = "PDF", "PDF"
    JPEG = "JPEG", "JPEG"
    PNG = "PNG", "PNG"


class Source(models.TextChoices):
    APP = "APP", "App"
    CLAUDE_LINK = "CLAUDE_LINK", "From Claude"


class Attachment(models.Model):
    """One file on one document. The file is named by its SHA-256 and never changes. Removing only marks it."""
    target_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_type", "target_id")
    file = models.FileField(upload_to="attachments/", max_length=255)
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=4, choices=Kind.choices)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.APP)
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    upload_link = models.ForeignKey("UploadLink", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="attachments")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    removed_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["uploaded_at", "id"]
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        return self.original_name


class UploadLink(models.Model):
    """A one-time page for adding files from Claude: for a previewed change, or for an existing document."""
    token = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    pending_change = models.ForeignKey("mcpserver.PendingChange", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="upload_links")
    target_type = models.ForeignKey(ContentType, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    target_id = models.PositiveBigIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_type", "target_id")
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()


auditlog.register(Attachment)

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class ZohoRecord(models.Model):
    """Traces one Zoho row (by its Zoho ID) to the document or entry it became."""

    kind = models.CharField(max_length=40)
    zoho_id = models.CharField(max_length=100)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    class Meta:
        unique_together = (("kind", "zoho_id"),)

    def __str__(self):
        return f"{self.kind} {self.zoho_id}"


class ImportRun(models.Model):
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    dry_run = models.BooleanField(default=False)
    export_name = models.CharField(max_length=255, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    summary = models.JSONField(default=dict, blank=True)
    report_pdf = models.FileField(upload_to="migration/", blank=True)
    succeeded = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.export_name} {self.started_at:%Y-%m-%d %H:%M}"

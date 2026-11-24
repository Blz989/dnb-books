from django.conf import settings
from django.db import models


class BackupRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Waiting"
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    filename = models.CharField(max_length=200, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    ledger_fingerprint = models.CharField(max_length=64, blank=True)
    error = models.TextField(blank=True)
    # Live progress, shown on the home screen while the backup runs.
    step = models.CharField(max_length=20, blank=True)  # the step running now, or the one a failed run stopped in
    progress = models.PositiveSmallIntegerField(default=0)  # percent
    detail = models.CharField(max_length=300, blank=True)
    step_times = models.JSONField(default=list, blank=True)  # [[step, seconds], ...] in the order they ran
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.started_at:%Y-%m-%d %H:%M} {self.status}"

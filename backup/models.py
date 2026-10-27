from django.db import models


class BackupRun(models.Model):
    class Status(models.TextChoices):
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

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"{self.started_at:%Y-%m-%d %H:%M} {self.status}"

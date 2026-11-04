"""Claude (MCP) access: the service switch, per-user access, pending previews, and the call log."""
from django.conf import settings
from django.db import models

USER = settings.AUTH_USER_MODEL


class McpSettings(models.Model):
    enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Claude connection settings"

    @classmethod
    def load(cls) -> "McpSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class McpAccess(models.Model):
    """A user may use DNB Books from Claude only when an admin turns this on."""
    user = models.OneToOneField(USER, on_delete=models.CASCADE, primary_key=True, related_name="mcp_access")
    enabled = models.BooleanField(default=False)
    changed_at = models.DateTimeField(auto_now=True)


class PendingChange(models.Model):
    """A previewed write. It posts only when Claude confirms it, once, before it expires."""
    id = models.CharField(max_length=40, primary_key=True)
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    tool = models.CharField(max_length=50)
    payload = models.JSONField()
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    document = models.CharField(max_length=60, blank=True)


class McpCall(models.Model):
    user = models.ForeignKey(USER, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    email = models.CharField(max_length=254, blank=True)
    tool = models.CharField(max_length=50)
    at = models.DateTimeField(auto_now_add=True)
    ok = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    document = models.CharField(max_length=60, blank=True)
    arguments = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at", "-id"]

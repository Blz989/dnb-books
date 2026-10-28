from auditlog.registry import auditlog
from django.conf import settings
from django.db import models
from django.db.models import Q

from contacts.models import Customer, Vendor
from ledger.models import Account
from purchasing.models import Bill, VendorCredit
from sales.models import Invoice, SalesOrder

USER = settings.AUTH_USER_MODEL


def long_date(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


class HandlingSettings(models.Model):
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    expense_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    start_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "handling settings"

    @classmethod
    def load(cls) -> "HandlingSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_configured(self) -> bool:
        return (
            self.vendor_id is not None
            and self.expense_account_id is not None
            and self.start_date is not None
            and HandlingRate.objects.exists()
        )

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class HandlingRate(models.Model):
    effective_from = models.DateField(unique=True)
    rate = models.DecimalField(max_digits=6, decimal_places=4)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-effective_from"]

    def __str__(self):
        return f"{self.rate} from {self.effective_from}"


class RunStatus(models.TextChoices):
    POSTED = "POSTED", "Posted"
    VOID = "VOID", "Void"


class HandlingRun(models.Model):
    period_start = models.DateField()
    period_end = models.DateField()
    total = models.DecimalField(max_digits=14, decimal_places=2)
    bill = models.ForeignKey(Bill, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    vendor_credit = models.ForeignKey(VendorCredit, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.POSTED)
    statement_pdf = models.FileField(upload_to="handling/")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    voided_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-period_start"]
        constraints = [
            # A voided period can be billed again, so only POSTED runs are unique.
            models.UniqueConstraint(
                fields=["period_start"], condition=Q(status="POSTED"), name="one_posted_run_per_period"
            ),
        ]

    @property
    def label(self) -> str:
        s, e = self.period_start, self.period_end
        if (s.year, s.month) == (e.year, e.month):
            return f"{s:%b} {s.day}–{e.day}, {e.year}"
        return f"{long_date(s)}–{long_date(e)}"

    def __str__(self):
        return self.label


class HandlingRunLine(models.Model):
    run = models.ForeignKey(HandlingRun, on_delete=models.CASCADE, related_name="lines")
    order_key = models.CharField(max_length=30)
    sales_order = models.ForeignKey(SalesOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="+")
    sales = models.DecimalField(max_digits=14, decimal_places=2)
    cost = models.DecimalField(max_digits=14, decimal_places=2)
    profit = models.DecimalField(max_digits=14, decimal_places=2)
    rate = models.DecimalField(max_digits=6, decimal_places=4)
    owed_to_date = models.DecimalField(max_digits=14, decimal_places=2)
    billed_before = models.DecimalField(max_digits=14, decimal_places=2)
    this_bill = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["order_key"]


for _model in (HandlingSettings, HandlingRate, HandlingRun, HandlingRunLine):
    auditlog.register(_model)

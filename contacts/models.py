from datetime import date, timedelta

from auditlog.registry import auditlog
from django.db import models
from django.db.models.functions import Lower

from ledger.models import Account, AccountType


class Terms(models.TextChoices):
    DUE_ON_RECEIPT = "DUE_ON_RECEIPT", "Due on receipt"
    NET_15 = "NET_15", "Net 15"
    NET_30 = "NET_30", "Net 30"
    NET_45 = "NET_45", "Net 45"
    NET_60 = "NET_60", "Net 60"


TERM_DAYS = {
    Terms.DUE_ON_RECEIPT: 0, Terms.NET_15: 15, Terms.NET_30: 30, Terms.NET_45: 45, Terms.NET_60: 60,
}


def due_date(terms: Terms, on: date) -> date:
    return on + timedelta(days=TERM_DAYS[Terms(terms)])


def duplicate_name_message(name: str) -> str:
    return f"A customer named {name} already exists."


class Customer(models.Model):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    billing_address = models.TextField(blank=True)
    ship_to_address = models.TextField(blank=True)
    terms = models.CharField(max_length=20, choices=Terms.choices, default=Terms.NET_30)
    resale_certificate = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="customer_name_ci_unique")]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError

        self.name = (self.name or "").strip()
        if self.name and Customer.objects.filter(name__iexact=self.name).exclude(pk=self.pk).exists():
            raise ValidationError({"name": duplicate_name_message(self.name)})

    def save(self, *args, **kwargs):
        self.full_clean(validate_constraints=False)
        super().save(*args, **kwargs)


def mask_tax_id(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isalnum())
    return f"•••-••-{digits[-4:]}" if digits else ""


def _normalize_name(name: str) -> str:
    return " ".join((name or "").split()).casefold()


class Vendor(models.Model):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    terms = models.CharField(max_length=20, choices=Terms.choices, default=Terms.NET_30)
    default_expense_account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_1099 = models.BooleanField(default=False)
    tax_id = models.CharField(max_length=30, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="vendor_name_ci_unique")]

    def __str__(self):
        return self.name

    @property
    def masked_tax_id(self) -> str:
        return mask_tax_id(self.tax_id)

    @classmethod
    def find_by_name(cls, name: str) -> "Vendor | None":
        """Match ignoring case, outer spaces, and runs of inner spaces."""
        wanted = _normalize_name(name)
        if not wanted:
            return None
        for vendor in cls.objects.all():
            if _normalize_name(vendor.name) == wanted:
                return vendor
        return None

    def clean(self):
        from django.core.exceptions import ValidationError

        self.name = (self.name or "").strip()
        if self.name and Vendor.objects.filter(name__iexact=self.name).exclude(pk=self.pk).exists():
            raise ValidationError({"name": f"A vendor named {self.name} already exists."})
        account = self.default_expense_account
        if account is not None and account.type != AccountType.EXPENSE:
            raise ValidationError({"default_expense_account": "Choose an expense account."})

    def save(self, *args, **kwargs):
        self.full_clean(validate_constraints=False)
        super().save(*args, **kwargs)


auditlog.register(Customer)
auditlog.register(Vendor)

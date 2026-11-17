import calendar
from datetime import date, timedelta

from auditlog.registry import auditlog
from django.db import models
from django.db.models.functions import Lower

from ledger.models import Account, AccountType


class Terms:
    """Codes of the five terms every install starts with. Other terms are created in Settings."""
    DUE_ON_RECEIPT = "DUE_ON_RECEIPT"
    NET_15 = "NET_15"
    NET_30 = "NET_30"
    NET_45 = "NET_45"
    NET_60 = "NET_60"


class PaymentTerm(models.Model):
    """A payment term. Documents store its code, so a term's code never changes once created.

    Either "net N days" (end_of_month False) or "end of the month, plus months_after months, plus days"
    (Due end of month = 0 months, Due end of next month = 1)."""
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    days = models.PositiveSmallIntegerField(default=0)
    end_of_month = models.BooleanField(default=False)
    months_after = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["end_of_month", "months_after", "days", "name"]

    def __str__(self):
        return self.name

    def due_on(self, on: date) -> date:
        if not self.end_of_month:
            return on + timedelta(days=self.days)
        index = on.year * 12 + on.month - 1 + self.months_after
        year, month = divmod(index, 12)
        month += 1
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last) + timedelta(days=self.days)

    @property
    def description(self) -> str:
        if not self.end_of_month:
            return "Due on the invoice date" if self.days == 0 else f"{self.days} days after the invoice date"
        when = "the end of the invoice month" if self.months_after == 0 else (
            "the end of next month" if self.months_after == 1 else f"the end of the month, {self.months_after} months on")
        return f"Due {when}" + (f", plus {self.days} days" if self.days else "")


def due_date(terms: str, on: date) -> date:
    try:
        term = PaymentTerm.objects.get(code=terms)
    except PaymentTerm.DoesNotExist:
        raise ValueError(f"Unknown payment terms '{terms}'.")
    return term.due_on(on)


def term_name(code: str) -> str:
    return PaymentTerm.objects.filter(code=code).values_list("name", flat=True).first() or code


class HasTerms:
    """Adds get_terms_display() for models that store a PaymentTerm code in `terms`."""
    def get_terms_display(self) -> str:
        return term_name(self.terms)


def duplicate_name_message(name: str) -> str:
    return f"A customer named {name} already exists."


class Customer(HasTerms, models.Model):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    billing_address = models.TextField(blank=True)
    ship_to_address = models.TextField(blank=True)
    terms = models.CharField(max_length=20, default=Terms.NET_30)
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


class Vendor(HasTerms, models.Model):
    name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    terms = models.CharField(max_length=20, default=Terms.NET_30)
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

        self.name = " ".join((self.name or "").split())
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

from datetime import date, timedelta

from auditlog.registry import auditlog
from django.db import models
from django.db.models.functions import Lower


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


auditlog.register(Customer)

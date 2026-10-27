from auditlog.registry import auditlog
from django.conf import settings
from django.db import models
from django.db.models import Q

from contacts.models import Customer, Terms
from items.models import Item
from ledger.models import Account, JournalEntry

from .errors import SalesSettingsMissing

USER = settings.AUTH_USER_MODEL


class Fulfillment(models.TextChoices):
    STOCK = "STOCK", "Ship from stock"
    DROP_SHIP = "DROP_SHIP", "Drop ship"


class PaymentMethod(models.TextChoices):
    CHECK = "CHECK", "Check"
    CASH = "CASH", "Cash"
    ACH = "ACH", "ACH"
    WIRE = "WIRE", "Wire"


class RefundMethod(models.TextChoices):
    CHECK = "CHECK", "Check"
    ACH = "ACH", "ACH"


class DocStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    VOID = "VOID", "Void"


class OrderStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"


class DocType(models.TextChoices):
    SO = "SO", "Sales order"
    INV = "INV", "Invoice"
    PMT = "PMT", "Payment"
    CN = "CN", "Credit note"
    DEP = "DEP", "Deposit"
    REF = "REF", "Refund"


class SalesSettings(models.Model):
    ar_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    undeposited_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    returns_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    company_name = models.CharField(max_length=200, blank=True)
    company_address = models.TextField(blank=True)
    company_phone = models.CharField(max_length=50, blank=True)
    company_email = models.CharField(max_length=200, blank=True)
    logo = models.FileField(upload_to="logo/", blank=True)
    default_invoice_notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "sales settings"

    _ACCOUNT_FIELDS = ("ar_account", "undeposited_account", "returns_account")

    @classmethod
    def load(cls) -> "SalesSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_configured(self) -> bool:
        return all(getattr(self, f"{f}_id") for f in self._ACCOUNT_FIELDS)

    def require_configured(self) -> "SalesSettings":
        if not self.is_configured:
            raise SalesSettingsMissing("Set up sales accounts first.")
        return self

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class DocumentSequence(models.Model):
    doc_type = models.CharField(max_length=3, choices=DocType.choices, unique=True)
    prefix = models.CharField(max_length=20)
    next_number = models.PositiveIntegerField(default=1)
    padding = models.PositiveSmallIntegerField(default=5)

    def __str__(self):
        return f"{self.doc_type} {self.prefix}{self.next_number}"


class SalesOrder(models.Model):
    number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sales_orders")
    date = models.DateField()
    ship_to = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=OrderStatus.choices, default=OrderStatus.OPEN)
    closed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class SalesOrderLine(models.Model):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=14, decimal_places=4)
    fulfillment = models.CharField(max_length=10, choices=Fulfillment.choices, default=Fulfillment.STOCK)
    drop_ship_vendor = models.CharField(max_length=200, blank=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class Invoice(models.Model):
    number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="invoices")
    date = models.DateField()
    terms = models.CharField(max_length=20, choices=Terms.choices, default=Terms.NET_30)
    due_date = models.DateField()
    ship_to = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="invoices"
    )
    revision = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.OPEN)
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=14, decimal_places=4)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    so_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="invoice_lines"
    )
    fulfillment = models.CharField(max_length=10, choices=Fulfillment.choices, default=Fulfillment.STOCK)
    drop_ship_vendor = models.CharField(max_length=200, blank=True)
    stock_movement = models.ForeignKey(
        "items.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    drop_ship_billed = models.BooleanField(default=False)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class InvoiceRevision(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    edited_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    edited_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["revision"]
        constraints = [models.UniqueConstraint(fields=["invoice", "revision"], name="invoice_revision_unique")]


class Deposit(models.Model):
    number = models.CharField(max_length=30, unique=True)
    date = models.DateField()
    bank_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.OPEN)
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class Payment(models.Model):
    number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="payments")
    date = models.DateField()
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    reference = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    deposited_to = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    deposit = models.ForeignKey(Deposit, null=True, blank=True, on_delete=models.PROTECT, related_name="payments")
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.OPEN)
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class PaymentApplication(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="applications")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payment_applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    applied_on = models.DateField()
    removed = models.BooleanField(default=False)
    removed_on = models.DateField(null=True, blank=True)


class CreditNote(models.Model):
    number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credit_notes")
    date = models.DateField()
    reason = models.CharField(max_length=200, blank=True)
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.OPEN)
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class CreditNoteLine(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=500, blank=True)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["id"]


class CreditApplication(models.Model):
    credit_note = models.ForeignKey(CreditNote, on_delete=models.PROTECT, related_name="applications")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="credit_applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    applied_on = models.DateField()
    removed = models.BooleanField(default=False)
    removed_on = models.DateField(null=True, blank=True)


class Refund(models.Model):
    number = models.CharField(max_length=30, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="refunds")
    date = models.DateField()
    bank_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    method = models.CharField(max_length=10, choices=RefundMethod.choices)
    reference = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    source_payment = models.ForeignKey(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="refunds"
    )
    source_credit_note = models.ForeignKey(
        CreditNote, null=True, blank=True, on_delete=models.PROTECT, related_name="refunds"
    )
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=DocStatus.choices, default=DocStatus.OPEN)
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(source_payment__isnull=False) & Q(source_credit_note__isnull=True))
                | (Q(source_payment__isnull=True) & Q(source_credit_note__isnull=False)),
                name="refund_exactly_one_source",
            ),
        ]

    def __str__(self):
        return self.number

    @property
    def source(self):
        return self.source_payment or self.source_credit_note


for _model in (
    SalesSettings, DocumentSequence, SalesOrder, SalesOrderLine, Invoice, InvoiceLine, InvoiceRevision, Deposit,
    Payment, PaymentApplication, CreditNote, CreditNoteLine, CreditApplication, Refund,
):
    auditlog.register(_model)

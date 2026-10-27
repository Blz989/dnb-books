from auditlog.registry import auditlog
from django.conf import settings
from django.db import models

from contacts.models import Terms, Vendor
from items.models import Item
from ledger.models import Account, JournalEntry
from sales.models import DocStatus, InvoiceLine, OrderStatus, SalesOrder, SalesOrderLine

from .errors import PurchasingSettingsMissing

USER = settings.AUTH_USER_MODEL


class BillLineKind(models.TextChoices):
    STOCK = "STOCK", "Stock"
    DROP_SHIP = "DROP_SHIP", "Drop ship"
    NON_STOCK = "NON_STOCK", "Non-stock"


class VendorPaymentMethod(models.TextChoices):
    CHECK = "CHECK", "Check"
    ACH = "ACH", "ACH"
    CARD = "CARD", "Company card"


class VendorCreditKind(models.TextChoices):
    RETURN = "RETURN", "Return to vendor"
    ALLOWANCE = "ALLOWANCE", "Credit without a return"


class PurchasingSettings(models.Model):
    ap_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name_plural = "purchasing settings"

    @classmethod
    def load(cls) -> "PurchasingSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_configured(self) -> bool:
        return self.ap_account_id is not None

    def require_configured(self) -> "PurchasingSettings":
        if not self.is_configured:
            raise PurchasingSettingsMissing("Set up purchasing accounts first.")
        return self

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class Threshold1099(models.Model):
    year = models.PositiveIntegerField(unique=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["-year"]

    def __str__(self):
        return f"{self.year}: {self.amount}"


class PurchaseOrder(models.Model):
    number = models.CharField(max_length=30, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    date = models.DateField()
    expected_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=OrderStatus.choices, default=OrderStatus.OPEN)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="purchase_orders"
    )
    closed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.number


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=14, decimal_places=4)
    so_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="po_lines"
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class Bill(models.Model):
    number = models.CharField(max_length=30, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="bills")
    vendor_invoice_number = models.CharField(max_length=100, blank=True)
    date = models.DateField()
    terms = models.CharField(max_length=20, choices=Terms.choices, default=Terms.NET_30)
    due_date = models.DateField()
    notes = models.TextField(blank=True)
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.PROTECT, related_name="bills"
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


class BillItemLine(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="item_lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=14, decimal_places=4)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    po_line = models.ForeignKey(
        PurchaseOrderLine, null=True, blank=True, on_delete=models.PROTECT, related_name="bill_lines"
    )
    kind = models.CharField(max_length=10, choices=BillLineKind.choices)
    stock_movement = models.ForeignKey(
        "items.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class BillExpenseLine(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="expense_lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]


class BillRevision(models.Model):
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="revisions")
    revision = models.PositiveIntegerField()
    snapshot = models.JSONField()
    edited_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    edited_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["revision"]
        constraints = [models.UniqueConstraint(fields=["bill", "revision"], name="bill_revision_unique")]


class DropShipAllocation(models.Model):
    bill_item_line = models.ForeignKey(BillItemLine, on_delete=models.CASCADE, related_name="allocations")
    invoice_line = models.ForeignKey(InvoiceLine, on_delete=models.CASCADE, related_name="drop_ship_allocations")
    quantity = models.PositiveIntegerField()


class VendorPayment(models.Model):
    number = models.CharField(max_length=30, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="payments")
    date = models.DateField()
    method = models.CharField(max_length=10, choices=VendorPaymentMethod.choices)
    paid_from = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    check_number = models.CharField(max_length=30, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
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


class BillPaymentApplication(models.Model):
    payment = models.ForeignKey(VendorPayment, on_delete=models.PROTECT, related_name="applications")
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="payment_applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    applied_on = models.DateField()
    removed = models.BooleanField(default=False)
    removed_on = models.DateField(null=True, blank=True)


class VendorCredit(models.Model):
    number = models.CharField(max_length=30, unique=True)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="credits")
    date = models.DateField()
    kind = models.CharField(max_length=10, choices=VendorCreditKind.choices)
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


class VendorCreditLine(models.Model):
    credit = models.ForeignKey(VendorCredit, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    description = models.CharField(max_length=500, blank=True)
    quantity = models.PositiveIntegerField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    stock_movement = models.ForeignKey(
        "items.StockMovement", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["id"]


class VendorCreditApplication(models.Model):
    credit = models.ForeignKey(VendorCredit, on_delete=models.PROTECT, related_name="applications")
    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="credit_applications")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    applied_on = models.DateField()
    removed = models.BooleanField(default=False)
    removed_on = models.DateField(null=True, blank=True)


for _model in (
    PurchasingSettings, Threshold1099, PurchaseOrder, PurchaseOrderLine, Bill, BillItemLine, BillExpenseLine,
    BillRevision, DropShipAllocation, VendorPayment, BillPaymentApplication, VendorCredit, VendorCreditLine,
    VendorCreditApplication,
):
    auditlog.register(_model)

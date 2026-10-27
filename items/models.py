import re

from auditlog.registry import auditlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower

from ledger.models import Account, AccountSubtype, JournalEntry

from .errors import ItemStateError

SKU_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$"
SKU_MESSAGE = "SKU can use letters, numbers, dots, dashes, and underscores (up to 50)."
RESERVED_SKUS = {"new", "adjust", "adjustments", "counts", "import", "settings", "movements"}  # URL words
RESERVED_MESSAGE = "That SKU is reserved."
IMMUTABLE_MESSAGE = "Stock movements can't be changed. Void the movement instead."


class ItemType(models.TextChoices):
    STOCK = "STOCK", "Stock"
    NON_STOCK = "NON_STOCK", "Non-stock"


class CostingMethod(models.TextChoices):
    FIFO = "FIFO", "FIFO"
    WEIGHTED_AVERAGE = "WEIGHTED_AVERAGE", "Weighted average"


class MovementKind(models.TextChoices):
    OPENING = "OPENING", "Opening stock"
    ADJUSTMENT = "ADJUSTMENT", "Quantity adjustment"
    COUNT = "COUNT", "Physical count"
    VALUE_ADJUSTMENT = "VALUE_ADJUSTMENT", "Value adjustment"
    SALE = "SALE", "Sale"  # reserved for Phase 3
    RECEIPT = "RECEIPT", "Receipt"  # reserved for Phase 4


class Category(models.Model):
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "categories"
        constraints = [models.UniqueConstraint(Lower("name"), name="category_name_ci_unique")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Item(models.Model):
    sku = models.CharField(max_length=50, blank=True)  # blank allowed here so clean() gives the SKU message
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    item_type = models.CharField(max_length=10, choices=ItemType.choices, default=ItemType.STOCK)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT, related_name="items")
    unit = models.CharField(max_length=30, blank=True, default="each")
    sale_price = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    purchase_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    vendor_part_number = models.CharField(max_length=100, blank=True)
    costing_method = models.CharField(max_length=20, choices=CostingMethod.choices, blank=True)
    income_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    cogs_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    inventory_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sku"]
        constraints = [models.UniqueConstraint(Lower("sku"), name="item_sku_ci_unique")]

    def __str__(self):
        return self.sku

    @property
    def is_stock(self) -> bool:
        return self.item_type == ItemType.STOCK

    @property
    def has_movements(self) -> bool:
        return self.pk is not None and self.movements.exists()

    def clean(self):
        errors = {}
        if not re.match(SKU_PATTERN, self.sku or ""):
            errors["sku"] = SKU_MESSAGE
        elif self.sku.lower() in RESERVED_SKUS:
            errors["sku"] = RESERVED_MESSAGE
        if self.is_stock:
            if self.cogs_account_id is None or self.cogs_account.subtype != AccountSubtype.COST_OF_GOODS_SOLD:
                errors["cogs_account"] = "Stock items need a cost of goods sold account."
            if self.inventory_account_id is None or self.inventory_account.subtype != AccountSubtype.INVENTORY:
                errors["inventory_account"] = "Stock items need an inventory account."
            if not self.costing_method:
                errors["costing_method"] = "Stock items need a costing method."
        else:
            if self.cogs_account_id or self.inventory_account_id or self.costing_method:
                errors["item_type"] = "Non-stock items can't have inventory accounts or a costing method."
        if self.income_account_id and self.income_account.subtype not in (
            AccountSubtype.INCOME, AccountSubtype.OTHER_INCOME
        ):
            errors["income_account"] = "The income account must be an income account."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class StockMovement(models.Model):
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    date = models.DateField()
    kind = models.CharField(max_length=20, choices=MovementKind.choices)
    quantity = models.IntegerField()
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    offset_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=50, blank=True)
    source_id = models.CharField(max_length=50, blank=True)
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    count_line = models.ForeignKey(
        "StockCountLine", null=True, blank=True, on_delete=models.PROTECT, related_name="movements")
    voided = models.BooleanField(default=False)
    voided_on = models.DateField(null=True, blank=True)
    void_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    void_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["item", "date", "id"], name="movement_item_date_idx")]

    _VOID_FIELDS = ("voided", "voided_on", "void_amount", "void_entry_id")
    _FROZEN = (
        "item_id", "date", "kind", "quantity", "unit_cost", "total_cost", "offset_account_id", "reason",
        "source_type", "source_id", "journal_entry_id", "count_line_id", "created_by_id",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            stored = StockMovement.objects.filter(pk=self.pk).first()
            if stored is not None:
                only_void = (
                    not stored.voided and self.voided
                    and all(getattr(stored, f) == getattr(self, f) for f in self._FROZEN)
                )
                if not only_void:
                    raise ItemStateError(IMMUTABLE_MESSAGE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ItemStateError(IMMUTABLE_MESSAGE)


class CostRevision(models.Model):
    movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, related_name="revisions")
    date = models.DateField()
    delta = models.DecimalField(max_digits=14, decimal_places=2)
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]

    def save(self, *args, **kwargs):
        if self.pk and CostRevision.objects.filter(pk=self.pk).exists():
            raise ItemStateError(IMMUTABLE_MESSAGE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ItemStateError(IMMUTABLE_MESSAGE)


class FifoLayer(models.Model):
    """A cache rebuilt by replay; never edited by hand."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="fifo_layers")
    source_movement = models.ForeignKey(StockMovement, on_delete=models.PROTECT, related_name="+")
    original_quantity = models.IntegerField()
    remaining_quantity = models.IntegerField()
    remaining_value = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ["source_movement__date", "source_movement_id"]


class CountScope(models.TextChoices):
    ALL = "ALL", "All items"
    CATEGORY = "CATEGORY", "One category"


class CountStatus(models.TextChoices):
    OPEN = "OPEN", "open"
    POSTED = "POSTED", "posted"
    CANCELED = "CANCELED", "canceled"


class StockCount(models.Model):
    name = models.CharField(max_length=100)
    scope = models.CharField(max_length=10, choices=CountScope.choices, default=CountScope.ALL)
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=CountStatus.choices, default=CountStatus.OPEN)
    started_at = models.DateTimeField()
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return self.name


class StockCountLine(models.Model):
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="+")
    expected_quantity = models.IntegerField()
    counted_quantity = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["item__sku"]
        constraints = [models.UniqueConstraint(fields=["count", "item"], name="count_line_unique_item")]


class InventorySettings(models.Model):
    default_inventory_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    default_cogs_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    default_income_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    shrinkage_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    writedown_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    opening_equity_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        verbose_name_plural = "inventory settings"

    _ACCOUNT_FIELDS = (
        "default_inventory_account", "default_cogs_account", "default_income_account",
        "shrinkage_account", "writedown_account", "opening_equity_account",
    )

    @classmethod
    def load(cls) -> "InventorySettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_configured(self) -> bool:
        return all(getattr(self, f"{f}_id") for f in self._ACCOUNT_FIELDS)

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


for _model in (Category, Item, StockCount, StockCountLine, InventorySettings):
    auditlog.register(_model)

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from auditlog.registry import auditlog

from .errors import ImmutableEntryError

IMMUTABLE_MESSAGE = "Posted entries can't be changed. Void it and create a new entry."


class AccountType(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    EXPENSE = "EXPENSE", "Expense"


class AccountSubtype(models.TextChoices):
    BANK = "BANK", "Bank"
    ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE", "Accounts receivable"
    INVENTORY = "INVENTORY", "Inventory"
    OTHER_CURRENT_ASSET = "OTHER_CURRENT_ASSET", "Other current asset"
    FIXED_ASSET = "FIXED_ASSET", "Fixed asset"
    OTHER_ASSET = "OTHER_ASSET", "Other asset"
    ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE", "Accounts payable"
    CREDIT_CARD = "CREDIT_CARD", "Credit card"
    OTHER_CURRENT_LIABILITY = "OTHER_CURRENT_LIABILITY", "Other current liability"
    LONG_TERM_LIABILITY = "LONG_TERM_LIABILITY", "Long term liability"
    EQUITY = "EQUITY", "Equity"
    INCOME = "INCOME", "Income"
    OTHER_INCOME = "OTHER_INCOME", "Other income"
    COST_OF_GOODS_SOLD = "COST_OF_GOODS_SOLD", "Cost of goods sold"
    EXPENSE = "EXPENSE", "Expense"
    OTHER_EXPENSE = "OTHER_EXPENSE", "Other expense"


SUBTYPES_BY_TYPE = {
    AccountType.ASSET: {
        AccountSubtype.BANK, AccountSubtype.ACCOUNTS_RECEIVABLE, AccountSubtype.INVENTORY,
        AccountSubtype.OTHER_CURRENT_ASSET, AccountSubtype.FIXED_ASSET, AccountSubtype.OTHER_ASSET,
    },
    AccountType.LIABILITY: {
        AccountSubtype.ACCOUNTS_PAYABLE, AccountSubtype.CREDIT_CARD,
        AccountSubtype.OTHER_CURRENT_LIABILITY, AccountSubtype.LONG_TERM_LIABILITY,
    },
    AccountType.EQUITY: {AccountSubtype.EQUITY},
    AccountType.INCOME: {AccountSubtype.INCOME, AccountSubtype.OTHER_INCOME},
    AccountType.EXPENSE: {AccountSubtype.COST_OF_GOODS_SOLD, AccountSubtype.EXPENSE, AccountSubtype.OTHER_EXPENSE},
}


class Account(models.Model):
    number = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=AccountType.choices)
    subtype = models.CharField(max_length=30, choices=AccountSubtype.choices)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"{self.number} {self.name}"

    def clean(self):
        if self.type in AccountType.values and self.subtype not in SUBTYPES_BY_TYPE[AccountType(self.type)]:
            raise ValidationError({"subtype": "That subtype doesn't belong to this account type."})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class EntryStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted"
    VOID = "VOID", "Void"


# Fields that must not change once an entry is no longer a draft.
_FROZEN_FIELDS = (
    "date", "reference", "memo", "source_type", "source_id",
    "reverses_id", "created_by_id", "posted_by_id", "posted_at",
)


class JournalEntry(models.Model):
    date = models.DateField()
    reference = models.CharField(max_length=50, blank=True)
    memo = models.TextField(blank=True)
    source_type = models.CharField(max_length=50, blank=True)
    source_id = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=10, choices=EntryStatus.choices, default=EntryStatus.DRAFT)
    reverses = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "id"]
        verbose_name_plural = "journal entries"

    def __str__(self):
        return self.display_number

    @property
    def display_number(self) -> str:
        return f"JE-{self.pk:06d}"

    def save(self, *args, **kwargs):
        if self.pk:
            stored = JournalEntry.objects.filter(pk=self.pk).first()
            if stored is not None and stored.status != EntryStatus.DRAFT:
                only_void = stored.status == EntryStatus.POSTED and self.status in (
                    EntryStatus.POSTED, EntryStatus.VOID
                )
                unchanged = all(getattr(stored, f) == getattr(self, f) for f in _FROZEN_FIELDS)
                if not (only_void and unchanged):
                    raise ImmutableEntryError(IMMUTABLE_MESSAGE)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        stored = JournalEntry.objects.filter(pk=self.pk).first()
        if stored is not None and stored.status != EntryStatus.DRAFT:
            raise ImmutableEntryError(IMMUTABLE_MESSAGE)
        return super().delete(*args, **kwargs)


class JournalLineQuerySet(models.QuerySet):
    def in_ledger(self):
        # VOID originals stay in the ledger; their reversal nets them to zero.
        return self.filter(entry__status__in=[EntryStatus.POSTED, EntryStatus.VOID])


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    description = models.CharField(max_length=200, blank=True)

    objects = JournalLineQuerySet.as_manager()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0), name="line_debit_non_negative"),
            models.CheckConstraint(condition=Q(credit__gte=0), name="line_credit_non_negative"),
            models.CheckConstraint(
                condition=(Q(debit__gt=0) & Q(credit=0)) | (Q(debit=0) & Q(credit__gt=0)),
                name="line_exactly_one_side",
            ),
        ]

    def _assert_parent_is_draft(self):
        status = JournalEntry.objects.filter(pk=self.entry_id).values_list("status", flat=True).first()
        if status is not None and status != EntryStatus.DRAFT:
            raise ImmutableEntryError(IMMUTABLE_MESSAGE)

    def save(self, *args, **kwargs):
        self._assert_parent_is_draft()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self._assert_parent_is_draft()
        return super().delete(*args, **kwargs)


class AccountingPeriod(models.Model):
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    is_closed = models.BooleanField(default=False)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = models.TextField(blank=True)
    close_override_reason = models.TextField(blank=True)
    close_override_problems = models.TextField(blank=True)

    class Meta:
        ordering = ["year", "month"]
        constraints = [models.UniqueConstraint(fields=["year", "month"], name="unique_period")]

    def __str__(self):
        return f"{self.year}-{self.month:02d}"


for _model in (Account, JournalEntry, JournalLine, AccountingPeriod):
    auditlog.register(_model)

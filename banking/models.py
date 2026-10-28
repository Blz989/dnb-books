from datetime import date

from auditlog.registry import auditlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from ledger.models import Account, AccountSubtype, JournalEntry, JournalLine
from purchasing.models import VendorPayment
from sales.models import Deposit, Payment, Refund

from .crypto import decrypt, encrypt

USER = settings.AUTH_USER_MODEL


class AccountKind(models.TextChoices):
    CHECKING = "CHECKING", "Checking"
    SAVINGS = "SAVINGS", "Savings"
    CARD = "CARD", "Credit card"
    LOAN = "LOAN", "Loan or line of credit"


class TxnStatus(models.TextChoices):
    NEW = "NEW", "New"
    MATCHED = "MATCHED", "Matched"
    BOOKED = "BOOKED", "Booked"
    EXCLUDED = "EXCLUDED", "Excluded"


class TxnSource(models.TextChoices):
    PLAID = "PLAID", "Plaid"
    FILE = "FILE", "file"


class ItemStatus(models.TextChoices):
    OK = "OK", "OK"
    NEEDS_LOGIN = "NEEDS_LOGIN", "Needs login"
    ERROR = "ERROR", "Error"


class TransferKind(models.TextChoices):
    TRANSFER = "TRANSFER", "Transfer"
    CARD_PAYOFF = "CARD_PAYOFF", "Card payoff"
    LOAN_PAYMENT = "LOAN_PAYMENT", "Loan payment"


class TransferStatus(models.TextChoices):
    POSTED = "POSTED", "Posted"
    VOID = "VOID", "Void"


class ReconStatus(models.TextChoices):
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    FINISHED = "FINISHED", "Finished"
    UNDONE = "UNDONE", "Undone"


class CheckStatus(models.TextChoices):
    PRINTED = "PRINTED", "printed"
    SPOILED = "SPOILED", "spoiled"
    VOIDED = "VOIDED", "voided"
    HANDWRITTEN = "HANDWRITTEN", "handwritten"


class PatternType(models.TextChoices):
    CONTAINS = "CONTAINS", "Contains"
    STARTS_WITH = "STARTS_WITH", "Starts with"


_SUBTYPES_FOR_KIND = {
    AccountKind.CHECKING: {AccountSubtype.BANK},
    AccountKind.SAVINGS: {AccountSubtype.BANK},
    AccountKind.CARD: {AccountSubtype.CREDIT_CARD},
    AccountKind.LOAN: {
        AccountSubtype.OTHER_CURRENT_LIABILITY, AccountSubtype.LONG_TERM_LIABILITY,
        AccountSubtype.CREDIT_CARD,  # a line of credit may be kept on a credit card account
    },
}


class PlaidItem(models.Model):
    institution_name = models.CharField(max_length=200)
    plaid_item_id = models.CharField(max_length=100, blank=True)
    access_token = models.TextField(blank=True)  # encrypted; use get_access_token()
    cursor = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=ItemStatus.choices, default=ItemStatus.OK)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return self.institution_name

    def get_access_token(self) -> str:
        return decrypt(self.access_token)

    def set_access_token(self, value: str) -> None:
        self.access_token = encrypt(value)


class BankAccount(models.Model):
    ledger_account = models.OneToOneField(Account, on_delete=models.PROTECT, related_name="bank_account")
    kind = models.CharField(max_length=10, choices=AccountKind.choices)
    plaid_item = models.ForeignKey(PlaidItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="accounts")
    plaid_account_id = models.CharField(max_length=100, blank=True)
    mask = models.CharField(max_length=10, blank=True)
    csv_mapping = models.JSONField(default=dict, blank=True)
    next_check_number = models.PositiveIntegerField(default=1001)
    feed_starts = models.DateField(default=date(2026, 1, 1))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["ledger_account__number"]

    def __str__(self):
        return self.ledger_account.name

    @property
    def is_liability(self) -> bool:
        return self.kind in (AccountKind.CARD, AccountKind.LOAN)

    def clean(self):
        if self.kind in AccountKind.values and self.ledger_account_id:
            if self.ledger_account.subtype not in _SUBTYPES_FOR_KIND[AccountKind(self.kind)]:
                raise ValidationError({"kind": "That kind doesn't fit the ledger account's subtype."})


class BankTransaction(models.Model):
    """Amount is signed from the account's point of view: money in (or a liability's balance going down) is positive."""
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transactions")
    external_id = models.CharField(max_length=100)
    date = models.DateField()
    description = models.CharField(max_length=500)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    check_number = models.CharField(max_length=30, blank=True)
    source = models.CharField(max_length=10, choices=TxnSource.choices)
    status = models.CharField(max_length=10, choices=TxnStatus.choices, default=TxnStatus.NEW)
    exclude_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["bank_account", "external_id"], name="banktxn_unique_external_id"),
        ]

    def __str__(self):
        return f"{self.date} {self.description} {self.amount}"


_MATCH_TARGETS = ("journal_entry", "sales_payment", "deposit", "refund", "vendor_payment")


def _exactly_one(fields):
    clauses = []
    for chosen in fields:
        q = Q(**{f"{chosen}__isnull": False})
        for other in fields:
            if other != chosen:
                q &= Q(**{f"{other}__isnull": True})
        clauses.append(q)
    combined = clauses[0]
    for q in clauses[1:]:
        combined |= q
    return combined


class Match(models.Model):
    bank_transaction = models.OneToOneField(BankTransaction, on_delete=models.CASCADE, related_name="match")
    journal_entry = models.ForeignKey(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_matches")
    # One-to-one: a document can match only one bank transaction. A journal entry can match two (a transfer
    # is seen from both accounts), so that one stays a foreign key.
    sales_payment = models.OneToOneField(
        Payment, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_match")
    deposit = models.OneToOneField(Deposit, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_match")
    refund = models.OneToOneField(Refund, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_match")
    vendor_payment = models.OneToOneField(
        VendorPayment, null=True, blank=True, on_delete=models.PROTECT, related_name="bank_match")
    confirmed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=_exactly_one(_MATCH_TARGETS), name="match_exactly_one_target"),
        ]

    @property
    def target(self):
        for name in _MATCH_TARGETS:
            value = getattr(self, name)
            if value is not None:
                return value
        return None


class Rule(models.Model):
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    pattern = models.CharField(max_length=200)
    pattern_type = models.CharField(max_length=15, choices=PatternType.choices, default=PatternType.CONTAINS)
    bank_account = models.ForeignKey(BankAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="rules")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    memo = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.get_pattern_type_display()} {self.pattern}"

    def matches(self, description: str) -> bool:
        text, pattern = description.casefold(), self.pattern.casefold()
        if self.pattern_type == PatternType.STARTS_WITH:
            return text.startswith(pattern)
        return pattern in text


class Transfer(models.Model):
    date = models.DateField()
    from_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transfers_out")
    to_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="transfers_in")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    kind = models.CharField(max_length=15, choices=TransferKind.choices, default=TransferKind.TRANSFER)
    principal = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    interest = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    interest_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    journal_entry = models.ForeignKey(JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=TransferStatus.choices, default=TransferStatus.POSTED)
    voided_on = models.DateField(null=True, blank=True)
    void_reason = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return f"TR-{self.pk}"


class Reconciliation(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="reconciliations")
    statement_date = models.DateField()
    statement_balance = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=12, choices=ReconStatus.choices, default=ReconStatus.IN_PROGRESS)
    started_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    finished_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    finished_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    undone_at = models.DateTimeField(null=True, blank=True)
    undo_reason = models.CharField(max_length=200, blank=True)
    report_pdf = models.FileField(upload_to="reconciliations/", blank=True)

    class Meta:
        ordering = ["-statement_date", "-id"]

    def __str__(self):
        return f"{self.bank_account} {self.statement_date:%B %Y}"


class ReconciliationItem(models.Model):
    reconciliation = models.ForeignKey(Reconciliation, on_delete=models.CASCADE, related_name="items")
    journal_line = models.ForeignKey(JournalLine, on_delete=models.PROTECT, related_name="+")
    cleared = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["reconciliation", "journal_line"], name="reconitem_unique_line"),
        ]


class CheckLayout(models.Model):
    bank_account = models.OneToOneField(BankAccount, on_delete=models.CASCADE, related_name="check_layout")
    fields = models.JSONField(default=dict, blank=True)
    offset_x = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    offset_y = models.DecimalField(max_digits=4, decimal_places=2, default=0)


class CheckNumber(models.Model):
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="check_numbers")
    number = models.CharField(max_length=30)
    status = models.CharField(max_length=12, choices=CheckStatus.choices)
    vendor_payment = models.ForeignKey(
        VendorPayment, null=True, blank=True, on_delete=models.PROTECT, related_name="check_numbers")
    refund = models.ForeignKey(Refund, null=True, blank=True, on_delete=models.PROTECT, related_name="check_numbers")
    printed_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    printed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bank_account", "number"], name="checknumber_unique_per_account"),
        ]

    def __str__(self):
        return f"{self.bank_account} check {self.number}"


for _model in (
    BankAccount, BankTransaction, Match, Rule, Transfer, Reconciliation, ReconciliationItem, CheckLayout, CheckNumber,
):
    auditlog.register(_model)
auditlog.register(PlaidItem, exclude_fields=["access_token"])

import csv
import io

from django import forms

from ledger.forms import date_field
from ledger.models import Account, AccountSubtype, AccountType

from .models import BankAccount, Rule, TransferKind

LEDGER_SUBTYPES = (
    AccountSubtype.BANK, AccountSubtype.CREDIT_CARD, AccountSubtype.OTHER_CURRENT_LIABILITY,
    AccountSubtype.LONG_TERM_LIABILITY,
)
DATE_FORMATS = [
    ("%m/%d/%Y", "MM/DD/YYYY"), ("%Y-%m-%d", "YYYY-MM-DD"), ("%m/%d/%y", "MM/DD/YY"), ("%d/%m/%Y", "DD/MM/YYYY"),
]
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
TOO_LARGE = "The file is too large (5 MB limit)."
WRONG_TYPE = "Upload an OFX, QFX, or CSV file."


def ledger_choices(exclude_used: bool = True):
    accounts = Account.objects.filter(subtype__in=LEDGER_SUBTYPES, is_active=True).order_by("number")
    return accounts.filter(bank_account__isnull=True) if exclude_used else accounts


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["ledger_account", "kind", "mask", "next_check_number", "feed_starts", "is_active"]
        labels = {
            "ledger_account": "Ledger account", "kind": "Kind", "mask": "Last 4 digits",
            "next_check_number": "Next check number", "feed_starts": "Feed starts", "is_active": "Active",
        }
        help_texts = {
            "ledger_account": "The account in the chart of accounts that this bank, card, or loan posts to.",
            "next_check_number": "For checking accounts you print checks on.",
            "feed_starts": "Bank rows dated before this are set aside.",
        }
        widgets = {"feed_starts": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["feed_starts"].input_formats = ["%Y-%m-%d"]
        editing = bool(self.instance.pk)
        self.fields["ledger_account"].queryset = ledger_choices(exclude_used=not editing)
        self.fields["ledger_account"].empty_label = "Choose an account"
        if editing:
            self.fields["ledger_account"].disabled = True


class ImportUploadForm(forms.Form):
    file = forms.FileField(label="Statement file")
    review_mapping = forms.BooleanField(required=False, label="Review the CSV column mapping first")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(TOO_LARGE)
        if not f.name.lower().endswith((".ofx", ".qfx", ".csv")):
            raise forms.ValidationError(WRONG_TYPE)
        return f


def csv_headers(text: str) -> list[str]:
    first = next(csv.reader(io.StringIO(text.lstrip("﻿"))), [])
    return [h.strip() for h in first]


def guess_mapping(headers: list[str]) -> dict:
    def find(*words):
        for h in headers:
            if any(w in h.lower() for w in words):
                return h
        return ""

    amount = find("amount")
    return {
        "date": find("date"), "description": find("descri", "payee", "memo", "name"),
        "amount": amount, "debit": "" if amount else find("debit", "withdraw"),
        "credit": "" if amount else find("credit", "deposit"), "check_number": find("check"),
        "date_format": "%m/%d/%Y",
    }


class CsvMappingForm(forms.Form):
    date = forms.ChoiceField(label="Date column")
    description = forms.ChoiceField(label="Description column")
    amount = forms.ChoiceField(label="Amount column", required=False, help_text="One column with + and - amounts.")
    debit = forms.ChoiceField(label="Debit column", required=False, help_text="Money out, if the file splits it.")
    credit = forms.ChoiceField(label="Credit column", required=False, help_text="Money in, if the file splits it.")
    check_number = forms.ChoiceField(label="Check number column", required=False)
    date_format = forms.ChoiceField(label="Date format", choices=DATE_FORMATS)

    def __init__(self, *args, headers: list[str], **kwargs):
        super().__init__(*args, **kwargs)
        options = [("", "Not used")] + [(h, h) for h in headers if h]
        for name in ("date", "description", "amount", "debit", "credit", "check_number"):
            self.fields[name].choices = options
        self.fields["date"].choices = self.fields["description"].choices = [("", "Choose a column")] + options[1:]

    def clean(self):
        data = super().clean()
        for name, label in (("date", "date"), ("description", "description")):
            if not data.get(name) and name not in self.errors:
                self.add_error(name, f"Choose the {label} column.")
        if not data.get("amount") and not data.get("debit") and not data.get("credit"):
            raise forms.ValidationError("Choose the amount column, or the debit and credit columns.")
        return data


class RuleForm(forms.ModelForm):
    class Meta:
        model = Rule
        fields = ["pattern", "pattern_type", "bank_account", "account", "memo", "is_active"]
        labels = {
            "pattern": "Bank description", "pattern_type": "Matches when the description",
            "bank_account": "Only for this account", "account": "Book to", "memo": "Memo", "is_active": "Active",
        }
        help_texts = {
            "pattern": "Capitals don't matter. The first active rule that matches suggests its account.",
            "bank_account": "Leave blank to use it on every account.",
            "memo": "Used as the journal entry memo. Blank uses the bank's description.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].required = False
        self.fields["bank_account"].empty_label = "Every account"
        self.fields["account"].queryset = Account.objects.filter(is_active=True).order_by("number")
        self.fields["memo"].required = False
        if not self.instance.pk:
            self.fields["is_active"].initial = True

    def clean_pattern(self):
        pattern = self.cleaned_data["pattern"].strip()
        if not pattern:
            raise forms.ValidationError("Enter the text to look for.")
        return pattern


class TransferForm(forms.Form):
    from_txn = forms.IntegerField(required=False, widget=forms.HiddenInput)
    date = date_field("Date")
    kind = forms.ChoiceField(label="Kind", choices=TransferKind.choices)
    from_account = forms.ModelChoiceField(
        BankAccount.objects.none(), label="From", empty_label="Choose an account",
        error_messages={"required": "Choose the account the money comes from."})
    to_account = forms.ModelChoiceField(
        BankAccount.objects.none(), label="To", empty_label="Choose an account",
        error_messages={"required": "Choose the account the money goes to."})
    amount = forms.DecimalField(label="Amount", max_digits=14, decimal_places=2, min_value=0,
                                error_messages={"required": "Enter an amount.", "invalid": "Enter an amount like 250.00."})
    principal = forms.DecimalField(label="Principal", required=False, max_digits=14, decimal_places=2, min_value=0)
    interest = forms.DecimalField(label="Interest", required=False, max_digits=14, decimal_places=2, min_value=0)
    interest_account = forms.ModelChoiceField(
        Account.objects.none(), label="Interest account", required=False, empty_label="Choose an account")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = BankAccount.objects.filter(is_active=True).select_related("ledger_account")
        self.fields["from_account"].queryset = accounts
        self.fields["to_account"].queryset = accounts
        self.fields["interest_account"].queryset = Account.objects.filter(
            is_active=True, type=AccountType.EXPENSE).order_by("number")


class ReconcileStartForm(forms.Form):
    statement_date = date_field("Statement date")
    statement_balance = forms.DecimalField(
        label="Statement ending balance", max_digits=14, decimal_places=2,
        error_messages={"required": "Enter the balance printed on the statement.",
                        "invalid": "Enter the balance like 1250.00."})

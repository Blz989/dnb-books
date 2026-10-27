import csv
import io

from django import forms

from ledger.models import Account, AccountSubtype

from .models import BankAccount

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
        fields = ["ledger_account", "kind", "mask", "next_check_number", "is_active"]
        labels = {
            "ledger_account": "Ledger account", "kind": "Kind", "mask": "Last 4 digits",
            "next_check_number": "Next check number", "is_active": "Active",
        }
        help_texts = {
            "ledger_account": "The account in the chart of accounts that this bank, card, or loan posts to.",
            "next_check_number": "For checking accounts you print checks on.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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

from django import forms

from .models import Account, JournalLine

DATE_ERROR = "Enter a date like 2026-01-31."


def date_field(label):
    return forms.DateField(
        label=label,
        input_formats=["%Y-%m-%d"],
        error_messages={"invalid": DATE_ERROR, "required": DATE_ERROR},
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )


class AsOfForm(forms.Form):
    as_of = date_field("As of")


class RangeForm(forms.Form):
    start = date_field("From")
    end = date_field("To")

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise forms.ValidationError("The start date must be on or before the end date.")
        return data


class GeneralLedgerForm(RangeForm):
    account = forms.ModelChoiceField(
        Account.objects.all(), error_messages={"invalid_choice": "Choose an account.", "required": "Choose an account."}
    )
    field_order = ["account", "start", "end"]


class VoidForm(forms.Form):
    date = date_field("Reversal date")
    reason = forms.CharField(label="Reason", max_length=200, error_messages={"required": "Enter a reason."})


class ReopenForm(forms.Form):
    reason = forms.CharField(
        label="Reason", widget=forms.Textarea(attrs={"rows": 3}),
        error_messages={"required": "A reason is required to reopen a period."},
    )


class JournalLineForm(forms.ModelForm):
    class Meta:
        model = JournalLine
        fields = ["account", "debit", "credit", "description"]

    def clean(self):
        data = super().clean()
        debit, credit = data.get("debit"), data.get("credit")
        if debit is None or credit is None:
            return data
        if debit < 0 or credit < 0:
            raise forms.ValidationError("Amounts can't be negative.")
        if debit > 0 and credit > 0:
            raise forms.ValidationError("Enter a debit or a credit, not both.")
        if debit == 0 and credit == 0:
            raise forms.ValidationError("Enter an amount.")
        return data


class CloseAnywayForm(forms.Form):
    reason = forms.CharField(
        label="Reason", widget=forms.Textarea(attrs={"rows": 3}),
        error_messages={"required": "Enter a reason to close anyway."},
    )

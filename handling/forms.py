from django import forms
from django.db.models import Q

from contacts.models import Customer, Vendor
from ledger.forms import date_field
from ledger.models import Account, AccountType

from .models import HandlingRun, HandlingSettings, RunStatus

RATE_ERROR = "Enter a rate between 0 and 100%."
START_LOCKED = "The start date can't change after the first handling bill."


class OrderProfitabilityForm(forms.Form):
    """GET filters. The field names are `from` and `to`, so they are added here rather than declared."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from"] = date_field("From")
        self.fields["to"] = date_field("To")
        self.fields["customer"] = forms.ModelChoiceField(
            Customer.objects.order_by("name"), required=False, empty_label="All customers", label="Customer",
            error_messages={"invalid_choice": "Choose a customer."})
        self.fields["unbilled"] = forms.BooleanField(required=False, label="Has unbilled handling")

    def clean(self):
        data = super().clean()
        if data.get("from") and data.get("to") and data["from"] > data["to"]:
            raise forms.ValidationError("The start date must be on or before the end date.")
        return data


class HandlingSettingsForm(forms.ModelForm):
    class Meta:
        model = HandlingSettings
        fields = ["vendor", "expense_account", "start_date"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance
        vendor = self.fields["vendor"]
        vendor.queryset = Vendor.objects.filter(Q(is_active=True) | Q(pk=current.vendor_id)).order_by("name")
        vendor.label = "Handling vendor"
        vendor.required = True
        vendor.error_messages.update({"required": "Choose a vendor.", "invalid_choice": "Choose a vendor."})
        account = self.fields["expense_account"]
        account.queryset = Account.objects.filter(
            Q(type=AccountType.EXPENSE, is_active=True) | Q(pk=current.expense_account_id)).order_by("number")
        account.label = "Expense account"
        account.required = True
        account.error_messages.update({"required": "Choose an account.", "invalid_choice": "Choose an account."})
        start = date_field("Start date")
        start.help_text = "Orders dated before this day are not charged handling."
        self.fields["start_date"] = start
        self.locked = HandlingRun.objects.filter(status=RunStatus.POSTED).exists()
        if self.locked:
            start.help_text = "Locked: a handling bill has been created."

    def clean_start_date(self):
        value = self.cleaned_data["start_date"]
        if self.locked and value != self.instance.start_date:
            raise forms.ValidationError(START_LOCKED)
        return value


class AddRateForm(forms.Form):
    effective_from = date_field("Effective from")
    percent = forms.DecimalField(label="Rate (%)", error_messages={"required": RATE_ERROR, "invalid": RATE_ERROR})

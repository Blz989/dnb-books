from django import forms

from contacts.models import Vendor
from core.roles import is_admin
from ledger.models import Account, AccountSubtype, AccountType

from .balances import vendor_balance
from .models import PurchasingSettings


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = [
            "name", "contact_name", "email", "phone", "address", "terms", "default_expense_account", "is_1099",
            "tax_id", "notes",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3}), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["default_expense_account"]
        field.queryset = Account.objects.filter(type=AccountType.EXPENSE, is_active=True)
        field.required = False
        field.label = "Default expense account"
        field.error_messages["invalid_choice"] = "Choose an expense account."
        self.fields["is_1099"].label = "Gets a 1099"
        self.fields["tax_id"].required = False
        self.fields["tax_id"].label = "Tax ID"
        # Only an admin sees the full tax ID, and only here. Everyone else sees the last four digits.
        self._masked = ""
        if self.instance.pk and self.instance.tax_id and not is_admin(user):
            self._masked = self.instance.masked_tax_id
            self.initial["tax_id"] = self._masked
        if self.instance.pk:
            self.fields["is_active"] = forms.BooleanField(required=False, label="Active", initial=self.instance.is_active)

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if Vendor.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"A vendor named {name} already exists.")
        return name

    def clean_tax_id(self):
        value = (self.cleaned_data.get("tax_id") or "").strip()
        if self._masked and value in ("", self._masked):
            return self.instance.tax_id
        return value

    def clean(self):
        data = super().clean()
        if self.instance.pk and self.instance.is_active and data.get("is_active") is False:
            balance = vendor_balance(self.instance)
            if balance != 0:
                raise forms.ValidationError(
                    f"{self.instance.name} has a balance of {balance:,.2f}. Settle it before deactivating.")
        return data

    def save(self, commit=True):
        vendor = super().save(commit=False)
        if "is_active" in self.cleaned_data:
            vendor.is_active = self.cleaned_data["is_active"]
        if commit:
            vendor.save()
        return vendor


class PurchasingSettingsForm(forms.ModelForm):
    class Meta:
        model = PurchasingSettings
        fields = ["ap_account"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["ap_account"]
        field.queryset = Account.objects.filter(subtype=AccountSubtype.ACCOUNTS_PAYABLE, is_active=True)
        field.label = "Accounts payable account"
        field.required = True
        field.error_messages["required"] = "Choose an account."
        field.error_messages["invalid_choice"] = "Choose an account."


# ---- purchase orders and bills ----
from contacts.models import Terms  # noqa: E402
from ledger.forms import date_field  # noqa: E402


class PurchaseOrderHeaderForm(forms.Form):
    vendor = forms.ModelChoiceField(
        Vendor.objects.none(), error_messages={"required": "Choose a vendor.", "invalid_choice": "Choose a vendor."})
    date = date_field("Date")
    expected_date = forms.DateField(
        label="Expected", required=False, input_formats=["%Y-%m-%d"],
        error_messages={"invalid": "Enter a date like 2026-01-31."},
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, all_vendors: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.all() if all_vendors else Vendor.objects.filter(is_active=True)


class BillHeaderForm(forms.Form):
    vendor = forms.ModelChoiceField(
        Vendor.objects.none(), error_messages={"required": "Choose a vendor.", "invalid_choice": "Choose a vendor."})
    vendor_invoice_number = forms.CharField(label="Vendor invoice #", required=False, max_length=100)
    date = date_field("Bill date")
    terms = forms.ChoiceField(label="Terms", required=False, choices=[("", "Vendor's default")] + list(Terms.choices))
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(label="Reason for the change", required=False, max_length=200)
    field_order = ["vendor", "vendor_invoice_number", "date", "terms", "notes", "reason"]

    def __init__(self, *args, editing: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.all() if editing else Vendor.objects.filter(is_active=True)
        if not editing:
            del self.fields["reason"]


class BillFromPOForm(forms.Form):
    vendor_invoice_number = forms.CharField(label="Vendor invoice #", required=False, max_length=100)
    date = date_field("Bill date")
    terms = forms.ChoiceField(label="Terms", required=False, choices=[("", "Vendor's default")] + list(Terms.choices))
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))

from django import forms

from contacts.models import Vendor
from contacts.terms import TermChoiceField, term_choices
from core.roles import is_admin
from ledger.models import Account, AccountSubtype, AccountType

from .balances import vendor_balance
from .models import PurchasingSettings


class VendorForm(forms.ModelForm):
    terms = TermChoiceField()

    class Meta:
        model = Vendor
        fields = [
            "name", "contact_name", "email", "phone", "address", "terms", "default_expense_account", "is_1099",
            "tax_id", "notes",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3}), "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["terms"].choices = term_choices(include=self.instance.terms if self.instance.pk else None)
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
        fields = ["ap_account", "default_adjustment_account"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        adjustment = self.fields["default_adjustment_account"]
        adjustment.queryset = Account.objects.filter(is_active=True).exclude(
            subtype__in=[AccountSubtype.INVENTORY, AccountSubtype.ACCOUNTS_PAYABLE])
        adjustment.label = "Default adjustment account"
        adjustment.required = False
        adjustment.help_text = "Bill adjustments start at this account. You can pick another on any bill."
        adjustment.error_messages["invalid_choice"] = "Choose an account other than inventory or accounts payable."
        field = self.fields["ap_account"]
        field.queryset = Account.objects.filter(subtype=AccountSubtype.ACCOUNTS_PAYABLE, is_active=True)
        field.label = "Accounts payable account"
        field.required = True
        field.error_messages["required"] = "Choose an account."
        field.error_messages["invalid_choice"] = "Choose an account."


# ---- purchase orders and bills ----
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
    terms = TermChoiceField(required=False, blank_label="Vendor's default")
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
    terms = TermChoiceField(required=False, blank_label="Vendor's default")
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))


# ---- payments and credits ----
from sales.forms import amount_field  # noqa: E402


def pay_from_accounts():
    return Account.objects.filter(
        subtype__in=[AccountSubtype.BANK, AccountSubtype.CREDIT_CARD], is_active=True).order_by("number")


class PayDetailsForm(forms.Form):
    """What every payment needs besides the bills: a date, a method, and where the money comes from."""
    date = date_field("Payment date")
    method = forms.ChoiceField(label="Method", choices=[("CHECK", "Check"), ("ACH", "ACH"), ("CARD", "Company card")])
    paid_from = forms.ModelChoiceField(
        Account.objects.none(), required=False, label="Paid from",
        error_messages={"invalid_choice": "Choose the account the payment came from."})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["paid_from"].queryset = pay_from_accounts()


class PayFilterForm(forms.Form):
    vendor = forms.ModelChoiceField(Vendor.objects.none(), required=False, empty_label="All vendors", label="Vendor")
    through = forms.DateField(
        label="Due through", required=False, input_formats=["%Y-%m-%d"],
        error_messages={"invalid": "Enter a date like 2026-01-31."},
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.all()


class VendorPaymentForm(PayDetailsForm):
    vendor = forms.ModelChoiceField(
        Vendor.objects.none(), error_messages={"required": "Choose a vendor.", "invalid_choice": "Choose a vendor."})
    check_number = forms.CharField(label="Check number", required=False, max_length=30)
    reference = forms.CharField(label="Reference", required=False, max_length=100)
    amount = amount_field()
    field_order = ["vendor", "date", "method", "paid_from", "check_number", "reference", "amount"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.all()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        return amount


class VendorCreditHeaderForm(forms.Form):
    vendor = forms.ModelChoiceField(
        Vendor.objects.none(), error_messages={"required": "Choose a vendor.", "invalid_choice": "Choose a vendor."})
    date = date_field("Date")
    reason = forms.CharField(label="Reason", required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)


# ---- adjustment ----
from sales.forms import parse_adjustment  # noqa: E402


ADJUSTMENT_ACCOUNT_ERROR = "Adjustment: choose an account other than inventory or accounts payable."


class BillAdjustmentForm(forms.Form):
    """Prefix "adj". The account starts at Purchasing settings' default adjustment account."""
    amount = forms.CharField(
        label="Adjustment", required=False,
        widget=forms.TextInput(attrs={"inputmode": "decimal", "placeholder": "-10.00 or 25.00"}))
    account = forms.ModelChoiceField(
        Account.objects.none(), label="Adjustment account", required=False, empty_label="Choose an account",
        error_messages={"invalid_choice": ADJUSTMENT_ACCOUNT_ERROR})
    note = forms.CharField(label="Adjustment note", required=False, max_length=500,
                           widget=forms.TextInput(attrs={"placeholder": "Adjustment"}))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("prefix", "adj")
        super().__init__(*args, **kwargs)
        default = PurchasingSettings.load().default_adjustment_account
        self.fields["account"].queryset = Account.objects.filter(is_active=True).exclude(
            subtype__in=[AccountSubtype.INVENTORY, AccountSubtype.ACCOUNTS_PAYABLE]).order_by("number")
        if default is not None and not self.is_bound and "account" not in self.initial:
            self.initial["account"] = default.pk

    def clean_amount(self):
        return parse_adjustment(self.cleaned_data.get("amount"))

    def clean(self):
        data = super().clean()
        if data.get("amount") is not None and data.get("account") is None and "account" not in self.errors:
            self.add_error("account", "Adjustment: choose an account.")
        return data

    @classmethod
    def initial_for(cls, line) -> dict:
        if line is None:
            return {}
        return {"amount": f"{line.amount:.2f}", "account": line.account_id, "note": line.description}

    def spec(self):
        from .posting.bills import BillAdjustmentSpec

        amount = self.cleaned_data.get("amount")
        if amount is None:
            return None
        return BillAdjustmentSpec(self.cleaned_data["account"], amount,
                                  self.cleaned_data.get("note", "").strip() or "Adjustment")

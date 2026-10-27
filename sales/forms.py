from django import forms

from contacts.models import Customer, Terms
from ledger.forms import date_field
from ledger.models import Account, AccountSubtype

from .models import PaymentMethod, RefundMethod, SalesSettings

LOGO_MAX_BYTES = 1024 * 1024
LOGO_MESSAGE = "Logo must be a PNG or JPEG under 1 MB."
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"


def logo_extension(data: bytes) -> str | None:
    if data.startswith(PNG_MAGIC):
        return "png"
    if data.startswith(JPEG_MAGIC):
        return "jpg"
    return None


class SalesSettingsForm(forms.ModelForm):
    logo = forms.FileField(required=False, label="Logo", help_text="PNG or JPEG, up to 1 MB.")
    remove_logo = forms.BooleanField(required=False, label="Remove the current logo")

    class Meta:
        model = SalesSettings
        fields = [
            "ar_account", "undeposited_account", "returns_account", "company_name", "company_address",
            "company_phone", "company_email", "default_invoice_notes",
        ]
        widgets = {
            "company_address": forms.Textarea(attrs={"rows": 3}),
            "default_invoice_notes": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "ar_account": "Accounts receivable account",
            "undeposited_account": "Undeposited Funds account",
            "returns_account": "Sales returns and allowances account",
            "default_invoice_notes": "Default invoice notes",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = {
            "ar_account": [AccountSubtype.ACCOUNTS_RECEIVABLE],
            "undeposited_account": [AccountSubtype.OTHER_CURRENT_ASSET],
            "returns_account": [AccountSubtype.INCOME],
        }
        for name, subtypes in choices.items():
            self.fields[name].queryset = Account.objects.filter(subtype__in=subtypes, is_active=True)
            self.fields[name].required = True
            self.fields[name].error_messages["required"] = "Choose an account."
        self.fields["company_name"].required = False
        self.fields["company_address"].required = False
        self.fields["company_phone"].required = False
        self.fields["company_email"].required = False
        self.fields["default_invoice_notes"].required = False
        self.logo_data: bytes | None = None
        self.logo_extension: str | None = None

    def clean_logo(self):
        upload = self.cleaned_data.get("logo")
        if not upload:
            return upload
        if upload.size > LOGO_MAX_BYTES:
            raise forms.ValidationError(LOGO_MESSAGE)
        data = upload.read()
        extension = logo_extension(data)
        if extension is None:
            raise forms.ValidationError(LOGO_MESSAGE)
        self.logo_data, self.logo_extension = data, extension
        return upload


class OrderHeaderForm(forms.Form):
    customer = forms.ModelChoiceField(
        Customer.objects.none(),
        error_messages={"required": "Choose a customer.", "invalid_choice": "Choose a customer."})
    date = date_field("Date")
    ship_to = forms.CharField(
        label="Ship to", required=False, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Leave blank to use the customer's default ship-to address.")
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, all_customers: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.all() if all_customers else Customer.objects.filter(is_active=True)


class InvoiceHeaderForm(OrderHeaderForm):
    terms = forms.ChoiceField(label="Terms", required=False, choices=[("", "Customer's default")] + list(Terms.choices))
    reason = forms.CharField(label="Reason for the change", required=False, max_length=200)
    field_order = ["customer", "date", "terms", "ship_to", "notes", "reason"]

    def __init__(self, *args, editing: bool = False, **kwargs):
        super().__init__(*args, all_customers=editing, **kwargs)
        self.fields["ship_to"].help_text = "" if editing else self.fields["ship_to"].help_text
        if not editing:
            del self.fields["reason"]


class ShipmentHeaderForm(forms.Form):
    date = date_field("Invoice date")
    terms = forms.ChoiceField(label="Terms", required=False, choices=[("", "Customer's default")] + list(Terms.choices))
    ship_to = forms.CharField(label="Ship to", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    notes = forms.CharField(label="Notes", required=False, widget=forms.Textarea(attrs={"rows": 3}))


class VoidDocForm(forms.Form):
    date = date_field("Void date")
    reason = forms.CharField(label="Reason", max_length=200, error_messages={"required": "Enter a reason."})


def amount_field(label="Amount"):
    return forms.DecimalField(
        label=label, max_digits=14, decimal_places=2,
        error_messages={
            "invalid": "Enter an amount above zero.", "required": "Enter an amount above zero.",
            "max_decimal_places": "Amounts can have at most 2 decimal places.",
            "max_digits": "That amount is too large.", "max_whole_digits": "That amount is too large.",
        })


def bank_accounts():
    return Account.objects.filter(subtype=AccountSubtype.BANK, is_active=True)


class PaymentForm(forms.Form):
    customer = forms.ModelChoiceField(
        Customer.objects.none(),
        error_messages={"required": "Choose a customer.", "invalid_choice": "Choose a customer."})
    date = date_field("Date")
    method = forms.ChoiceField(label="Method", choices=PaymentMethod.choices)
    reference = forms.CharField(label="Check or transfer number", required=False, max_length=100)
    amount = amount_field()
    bank_account = forms.ModelChoiceField(
        bank_accounts(), required=False, label="Bank account", empty_label="Choose a bank account",
        help_text="Needed for ACH and wire. Checks and cash go to Undeposited Funds.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(is_active=True)
        self.fields["bank_account"].queryset = bank_accounts()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        return amount


class CreditNoteForm(forms.Form):
    customer = forms.ModelChoiceField(
        Customer.objects.none(),
        error_messages={"required": "Choose a customer.", "invalid_choice": "Choose a customer."})
    date = date_field("Date")
    reason = forms.CharField(label="Reason", required=False, max_length=200)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = Customer.objects.filter(is_active=True)


class RefundForm(forms.Form):
    date = date_field("Date")
    method = forms.ChoiceField(label="Method", choices=RefundMethod.choices)
    reference = forms.CharField(label="Check number or transfer reference", required=False, max_length=100)
    bank_account = forms.ModelChoiceField(
        bank_accounts(), label="Paid from", empty_label="Choose a bank account",
        error_messages={"required": "Choose the bank account the refund comes from.",
                        "invalid_choice": "Choose the bank account the refund comes from."})
    amount = amount_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = bank_accounts()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        return amount


class DepositForm(forms.Form):
    date = date_field("Deposit date")
    bank_account = forms.ModelChoiceField(
        bank_accounts(), label="Deposit to", empty_label="Choose a bank account",
        error_messages={"required": "Choose the bank account for the deposit.",
                        "invalid_choice": "Choose the bank account for the deposit."})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = bank_accounts()


class ApplyForm(forms.Form):
    date = date_field("Apply on")

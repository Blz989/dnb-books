from django import forms

from ledger.forms import date_field  # noqa: F401  (re-exported for the sales screens)
from ledger.models import Account, AccountSubtype

from .models import SalesSettings

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

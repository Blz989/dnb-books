from django import forms

from sales.balances import customer_balance

from .models import Customer, duplicate_name_message


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = [
            "name", "contact_name", "email", "phone", "billing_address", "ship_to_address", "terms",
            "resale_certificate", "notes",
        ]
        widgets = {
            "billing_address": forms.Textarea(attrs={"rows": 3}),
            "ship_to_address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ship_to_address"].label = "Default ship-to address"
        if self.instance.pk:
            self.fields["is_active"] = forms.BooleanField(required=False, label="Active", initial=self.instance.is_active)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if Customer.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(duplicate_name_message(name))
        return name

    def clean(self):
        data = super().clean()
        if self.instance.pk and self.instance.is_active and data.get("is_active") is False:
            balance = customer_balance(self.instance)
            if balance != 0:
                raise forms.ValidationError(
                    f"{self.instance.name} has a balance of {balance:,.2f}. Settle it before deactivating.")
        return data

    def save(self, commit=True):
        customer = super().save(commit=False)
        if "is_active" in self.cleaned_data:
            customer.is_active = self.cleaned_data["is_active"]
        if commit:
            customer.save()
        return customer

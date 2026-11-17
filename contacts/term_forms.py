from django import forms

from .models import PaymentTerm
from .terms import make_code, usage


class PaymentTermForm(forms.ModelForm):
    field_order = ["name", "rule", "days", "months_after", "is_active"]
    rule = forms.ChoiceField(
        label="Due date rule", choices=[("days", "A number of days after the document date"),
                                        ("eom", "The end of a month")], initial="days")

    class Meta:
        model = PaymentTerm
        fields = ["name", "days", "months_after", "is_active"]
        labels = {"name": "Name", "days": "Days", "months_after": "Months after the document month",
                  "is_active": "Active"}
        help_texts = {
            "days": "For a month-end term, extra days added after the month end (usually 0).",
            "months_after": "0 is the end of the document's own month, 1 is the end of next month.",
            "is_active": "Inactive terms are hidden from dropdowns. Records that already use one keep it.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].error_messages["required"] = "Enter a name."
        self.fields["days"].required = False
        self.fields["months_after"].required = False
        self.in_use = {}
        if self.instance.pk:
            self.fields["rule"].initial = "eom" if self.instance.end_of_month else "days"
            self.in_use = usage(self.instance)
            if self.in_use:
                for key in ("rule", "days", "months_after"):
                    self.fields[key].disabled = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if PaymentTerm.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"A term named {name} already exists.")
        return name

    def clean(self):
        data = super().clean()
        if self.fields["rule"].disabled:
            return data
        data["days"] = data.get("days") or 0
        data["months_after"] = (data.get("months_after") or 0) if data.get("rule") == "eom" else 0
        if data["days"] > 365:
            self.add_error("days", "Enter 365 days or fewer.")
        if data["months_after"] > 12:
            self.add_error("months_after", "Enter 12 months or fewer.")
        return data

    def save(self, commit=True):
        term = super().save(commit=False)
        if not self.fields["rule"].disabled:
            term.end_of_month = self.cleaned_data["rule"] == "eom"
            term.days = self.cleaned_data["days"]
            term.months_after = self.cleaned_data["months_after"]
        if not term.pk:
            term.code = make_code(term.name)
        if commit:
            term.save()
        return term

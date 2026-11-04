"""The Bank register report. The bank account page's All transactions tab uses the same form, rows, and table."""
from django import forms
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from ledger.csv_export import amt, csv_response
from ledger.forms import date_field
from ledger.views import _wants_csv

from ..models import BankAccount
from ..register import RECONCILED_CHOICES, TYPE_CHOICES, RegisterFilters, build_rows, filtered_lines

PAGE_SIZE = 50


class _AccountChoice(forms.ModelChoiceField):
    def label_from_instance(self, a):
        return f"{a.ledger_account.name} ···· {a.mask}" if a.mask else a.ledger_account.name


def _optional_date(label):
    field = date_field(label)
    field.required = False
    return field


class RegisterFilterForm(forms.Form):
    start = _optional_date("From")
    end = _optional_date("To")
    type = forms.ChoiceField(choices=TYPE_CHOICES, required=False, label="Type")
    q = forms.CharField(required=False, label="Search", widget=forms.TextInput(
        attrs={"type": "search", "placeholder": "Payee, reference, or amount"}))
    reconciled = forms.ChoiceField(choices=RECONCILED_CHOICES, required=False, label="Reconciled")

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            raise forms.ValidationError("The start date must be on or before the end date.")
        return data

    def filters(self) -> RegisterFilters:
        d = self.cleaned_data
        return RegisterFilters(start=d.get("start"), end=d.get("end"), type=d.get("type") or "",
                               q=d.get("q") or "", reconciled=d.get("reconciled") or "")


class BankRegisterForm(RegisterFilterForm):
    account = _AccountChoice(BankAccount.objects.select_related("ledger_account"), required=False, label="Account",
                             empty_label="Pick an account")
    field_order = ["account", "start", "end", "type", "q", "reconciled"]


def register_csv(account: BankAccount, rows):
    body = [[r.date.isoformat(), r.reference, r.type_label, r.counterparty, r.status,
             amt(r.deposit) if r.deposit is not None else "", amt(r.withdrawal) if r.withdrawal is not None else "",
             amt(r.balance)] for r in rows]
    return csv_response(f"bank-register-{account.ledger_account.number}.csv",
                        ["Date", "Reference", "Type", "Counterparty", "Status", "Deposit", "Withdrawal", "Balance"], body)


def register_page(request, account: BankAccount, form: RegisterFilterForm):
    """(rows, page) for the screen, all rows for CSV and PDF. page is None when not paginated."""
    lines = filtered_lines(account, form.filters())
    if _wants_csv(request) or getattr(request, "report_pdf", False):
        return build_rows(account, list(lines)), None
    page = Paginator(lines, PAGE_SIZE).get_page(request.GET.get("page"))
    return build_rows(account, list(page)), page


def querystring(request, drop=("page", "format")) -> str:
    params = request.GET.copy()
    for key in drop:
        params.pop(key, None)
    return params.urlencode()


@login_required
def bank_register(request):
    form = BankRegisterForm(request.GET or None)
    account = rows = page = None
    if form.is_valid() and form.cleaned_data["account"] is not None:
        account = form.cleaned_data["account"]
        rows, page = register_page(request, account, form)
        if _wants_csv(request):
            return register_csv(account, rows)
    return render(request, "banking/report_bank_register.html", {
        "form": form, "account": account, "rows": rows, "page": page, "querystring": querystring(request),
        "wide": True, "nav_active": "reports"})

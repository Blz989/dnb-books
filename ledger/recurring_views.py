"""Screens for recurring bills (Purchases) and recurring journal entries (Accountant). Spec 3.3."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from contacts.terms import TermChoiceField
from core.roles import is_admin

from .errors import PostingError
from .forms import date_field
from .models import Account, AccountSubtype, Frequency, RecurringJournal, RecurringJournalLine
from .posting import LineSpec, validate_lines
from .recurring import next_date

ZERO = Decimal("0.00")
LINE_ROWS = 20
VISIBLE_ROWS = 3


class RecurringError(Exception):
    pass


# ---------------------------------------------------------------- forms

class _ScheduleForm(forms.Form):
    name = forms.CharField(max_length=100, error_messages={"required": "Enter a name."})
    frequency = forms.ChoiceField(choices=Frequency.choices, initial=Frequency.MONTHLY)
    start_date = date_field("Start date")
    end_date = date_field("End date (optional)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["end_date"].required = False

    def clean(self):
        data = super().clean()
        if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
            raise forms.ValidationError("The end date can't be before the start date.")
        return data


class RecurringBillForm(_ScheduleForm):
    vendor = forms.ModelChoiceField(None, error_messages={"required": "Choose a vendor.",
                                                          "invalid_choice": "Choose a vendor."})
    terms = TermChoiceField(required=False, blank_label="Vendor's default")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    field_order = ["name", "vendor", "frequency", "start_date", "end_date", "terms", "notes"]

    def __init__(self, *args, **kwargs):
        from contacts.models import Vendor

        super().__init__(*args, **kwargs)
        self.fields["vendor"].queryset = Vendor.objects.filter(is_active=True)


class RecurringJournalForm(_ScheduleForm):
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    field_order = ["name", "frequency", "start_date", "end_date", "memo"]


# ---------------------------------------------------------------- lines

def _rows(kind, post=None, profile=None):
    fields = ("account", "description", "amount") if kind.is_bill else ("account", "description", "debit", "credit")
    rows = []
    existing = list(profile.lines.all()) if profile and not post else []
    for n in range(1, LINE_ROWS + 1):
        if post:
            row = {f: post.get(f"{f}_{n}", "").strip() for f in fields}
        elif n <= len(existing):
            l = existing[n - 1]
            row = {"account": str(l.account_id), "description": l.description}
            if kind.is_bill:
                row["amount"] = f"{l.amount:.2f}"
            else:
                row["debit"] = f"{l.debit:.2f}" if l.debit else ""
                row["credit"] = f"{l.credit:.2f}" if l.credit else ""
        else:
            row = {f: "" for f in fields}
        row["n"] = n
        rows.append(row)
    used = max([r["n"] for r in rows if any(r[f] for f in fields)] or [0])
    for r in rows:
        r["hidden"] = r["n"] > max(VISIBLE_ROWS, used)
    return rows


def _money(raw: str, n: int) -> Decimal:
    if not raw:
        return ZERO
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        raise RecurringError(f"Line {n}: enter an amount like 125.00.") from None
    if not value.is_finite() or value.as_tuple().exponent < -2:
        raise RecurringError(f"Line {n}: enter an amount like 125.00.")
    return value


def _used(rows, fields):
    return [r for r in rows if any(r[f] for f in fields)]


def bill_lines(rows) -> list[tuple[Account, str, Decimal]]:
    used = _used(rows, ("account", "description", "amount"))
    if not used:
        raise RecurringError("Add at least one line.")
    accounts = Account.objects.in_bulk([int(r["account"]) for r in used if r["account"].isdigit()])
    out = []
    for n, r in enumerate(used, 1):
        account = accounts.get(int(r["account"])) if r["account"].isdigit() else None
        if account is None:
            raise RecurringError(f"Line {n}: choose an account.")
        if account.subtype == AccountSubtype.ACCOUNTS_PAYABLE:
            raise RecurringError(f"Line {n}: choose an account other than accounts payable.")
        if account.subtype == AccountSubtype.INVENTORY:
            raise RecurringError(f"Line {n}: inventory changes through stock movements. Choose an expense account.")
        amount = _money(r["amount"], n)
        if amount <= 0:
            raise RecurringError(f"Line {n}: enter an amount above zero.")
        out.append((account, r["description"][:500], amount))
    return out


def journal_lines(rows) -> list[LineSpec]:
    used = _used(rows, ("account", "description", "debit", "credit"))
    if not used:
        raise RecurringError("Add at least one line.")
    accounts = Account.objects.in_bulk([int(r["account"]) for r in used if r["account"].isdigit()])
    specs = []
    for n, r in enumerate(used, 1):
        account = accounts.get(int(r["account"])) if r["account"].isdigit() else None
        if account is None:
            raise RecurringError(f"Line {n}: choose an account.")
        specs.append(LineSpec(account, _money(r["debit"], n), _money(r["credit"], n), r["description"][:200]))
    try:
        validate_lines(specs)
    except PostingError as exc:
        raise RecurringError(str(exc)) from None
    return specs


# ---------------------------------------------------------------- the two kinds

@dataclass(frozen=True)
class Kind:
    is_bill: bool
    nav: str
    title: str          # "Recurring bills"
    one: str            # "recurring bill"
    url: str            # url name prefix
    create_label: str

    @property
    def model(self):
        if self.is_bill:
            from purchasing.models import RecurringBill
            return RecurringBill
        return RecurringJournal

    def allowed(self, user) -> bool:
        if self.is_bill:
            return True
        return user.has_perm("ledger.add_journalentry") or is_admin(user)


BILLS = Kind(True, "recurring-bills", "Recurring bills", "recurring bill", "purchasing:recurring", "New recurring bill")
JOURNALS = Kind(False, "recurring-journals", "Recurring journal entries", "recurring journal entry",
                "books:recurring", "New recurring journal entry")


def _check(kind, request):
    if not kind.allowed(request.user):
        raise Http404


def _amount(kind, profile) -> Decimal:
    lines = list(profile.lines.all())
    if kind.is_bill:
        return sum((l.amount for l in lines), ZERO)
    return sum((l.debit for l in lines), ZERO)


def _ctx(kind, **extra):
    return {"kind": kind, "nav_active": kind.nav, **extra}


def recurring_list(request, kind):
    _check(kind, request)
    today = timezone.localdate()
    qs = kind.model.objects.prefetch_related("lines", "runs")
    if kind.is_bill:
        qs = qs.select_related("vendor")
    rows = [{"p": p, "amount": _amount(kind, p), "next": next_date(p, today) if p.is_active else None,
             "status": p.status(today)} for p in qs]
    return render(request, "books/recurring_list.html", _ctx(kind, rows=rows))


def recurring_detail(request, kind, pk):
    _check(kind, request)
    p = get_object_or_404(kind.model, pk=pk)
    today = timezone.localdate()
    runs = p.runs.select_related("bill" if kind.is_bill else "entry").order_by("-scheduled_date")
    return render(request, "books/recurring_detail.html", _ctx(
        kind, p=p, lines=p.lines.select_related("account"), runs=runs, amount=_amount(kind, p),
        next=next_date(p, today) if p.is_active else None, status=p.status(today),
        can_delete=not p.runs.exists()))


def _save(kind, request, profile=None):
    _check(kind, request)
    form_class = RecurringBillForm if kind.is_bill else RecurringJournalForm
    initial = {}
    if profile is not None:
        initial = {f: getattr(profile, f) for f in form_class.base_fields if hasattr(profile, f)}
        if kind.is_bill:
            initial["vendor"] = profile.vendor_id
    else:
        initial = {"start_date": timezone.localdate()}
    form = form_class(request.POST or None, initial=initial)
    rows = _rows(kind, request.POST if request.method == "POST" else None, profile)
    error = None
    if request.method == "POST" and form.is_valid():
        try:
            lines = bill_lines(rows) if kind.is_bill else journal_lines(rows)
        except RecurringError as exc:
            error = str(exc)
        else:
            with transaction.atomic():
                p = profile or kind.model(created_by=request.user)
                for f, v in form.cleaned_data.items():
                    setattr(p, f, v)
                p.save()
                p.lines.all().delete()
                for n, line in enumerate(lines):
                    if kind.is_bill:
                        from purchasing.models import RecurringBillLine
                        account, description, amount = line
                        RecurringBillLine.objects.create(profile=p, account=account, description=description,
                                                         amount=amount, position=n)
                    else:
                        RecurringJournalLine.objects.create(profile=p, account=line.account, debit=line.debit,
                                                            credit=line.credit, description=line.description,
                                                            position=n)
            messages.success(request, f"Saved {p.name}. It runs on its next date.")
            return redirect(reverse(f"{kind.url}-detail", args=[p.pk]))
    accounts = Account.objects.filter(is_active=True).order_by("number")
    if kind.is_bill:
        accounts = accounts.exclude(subtype__in=[AccountSubtype.ACCOUNTS_PAYABLE, AccountSubtype.INVENTORY])
    return render(request, "books/recurring_form.html", _ctx(
        kind, form=form, rows=rows, accounts=accounts, error=error, profile=profile))


def recurring_new(request, kind):
    return _save(kind, request)


def recurring_edit(request, kind, pk):
    _check(kind, request)
    return _save(kind, request, get_object_or_404(kind.model, pk=pk))


def recurring_pause(request, kind, pk):
    _check(kind, request)
    p = get_object_or_404(kind.model, pk=pk)
    p.is_active = False
    p.save(update_fields=["is_active"])
    messages.success(request, f"Paused {p.name}.")
    return redirect(reverse(f"{kind.url}-detail", args=[p.pk]))


def recurring_resume(request, kind, pk):
    _check(kind, request)
    p = get_object_or_404(kind.model, pk=pk)
    p.resume(timezone.localdate())
    messages.success(request, f"Resumed {p.name}. Dates passed while it was paused are skipped.")
    return redirect(reverse(f"{kind.url}-detail", args=[p.pk]))


def recurring_delete(request, kind, pk):
    _check(kind, request)
    p = get_object_or_404(kind.model, pk=pk)
    if p.runs.exists():
        error = "This schedule has created documents. Set an end date instead of deleting it."
        if request.method == "POST":
            today = timezone.localdate()
            return render(request, "books/recurring_detail.html", _ctx(
                kind, p=p, lines=p.lines.select_related("account"),
                runs=p.runs.select_related("bill" if kind.is_bill else "entry").order_by("-scheduled_date"),
                amount=_amount(kind, p), next=next_date(p, today) if p.is_active else None,
                status=p.status(today), can_delete=False, error=error))
    if request.method == "POST":
        name = p.name
        p.delete()
        messages.success(request, f"Deleted {name}.")
        return redirect(reverse(f"{kind.url}-list"))
    return redirect(reverse(f"{kind.url}-detail", args=[p.pk]))


def _bind(kind):
    def wrap(view, post_only=False):
        def inner(request, *args, **kwargs):
            return view(request, kind, *args, **kwargs)
        inner = login_required(inner)
        return require_POST(inner) if post_only else inner
    return {
        "list": wrap(recurring_list), "new": wrap(recurring_new), "detail": wrap(recurring_detail),
        "edit": wrap(recurring_edit), "pause": wrap(recurring_pause, True), "resume": wrap(recurring_resume, True),
        "delete": wrap(recurring_delete),
    }


def urls(kind, prefix: str = "recurring/"):
    from django.urls import path

    v = _bind(kind)
    name = kind.url.split(":")[1]
    return [
        path(prefix, v["list"], name=f"{name}-list"),
        path(f"{prefix}new/", v["new"], name=f"{name}-new"),
        path(f"{prefix}<int:pk>/", v["detail"], name=f"{name}-detail"),
        path(f"{prefix}<int:pk>/edit/", v["edit"], name=f"{name}-edit"),
        path(f"{prefix}<int:pk>/pause/", v["pause"], name=f"{name}-pause"),
        path(f"{prefix}<int:pk>/resume/", v["resume"], name=f"{name}-resume"),
        path(f"{prefix}<int:pk>/delete/", v["delete"], name=f"{name}-delete"),
    ]

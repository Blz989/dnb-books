"""Payment term choices for forms, and name/code lookup for imports and tools."""
from django import forms

from .models import PaymentTerm


def term_choices(blank_label: str | None = None, include: str | None = None) -> list[tuple[str, str]]:
    """Active terms for a dropdown. `include` keeps a term that was later deactivated selectable on the
    record that already uses it."""
    rows = [(t.code, t.name) for t in PaymentTerm.objects.all() if t.is_active or t.code == include]
    return ([("", blank_label)] if blank_label else []) + rows


class TermChoiceField(forms.ChoiceField):
    def __init__(self, *, blank_label: str | None = None, **kwargs):
        kwargs.setdefault("label", "Terms")
        kwargs["choices"] = lambda: term_choices(blank_label)
        super().__init__(**kwargs)


def find_term(value: str, *, active_only: bool = True) -> PaymentTerm | None:
    """A term by code or name, ignoring case."""
    text = (value or "").strip().lower()
    for term in PaymentTerm.objects.all():
        if text in (term.code.lower(), term.name.lower()):
            return term if (term.is_active or not active_only) else None
    return None


def default_term_code() -> str:
    """Net 30 for a new customer or vendor made by a tool, or the first active term when Net 30 is off."""
    from .models import Terms
    if PaymentTerm.objects.filter(code=Terms.NET_30, is_active=True).exists():
        return Terms.NET_30
    first = PaymentTerm.objects.filter(is_active=True).first()
    return first.code if first else Terms.NET_30


def usage(term: PaymentTerm) -> dict[str, int]:
    """How many records use a term, by kind."""
    from purchasing.models import Bill
    from sales.models import Invoice
    from .models import Customer, Vendor
    counts = {
        "customers": Customer.objects.filter(terms=term.code).count(),
        "vendors": Vendor.objects.filter(terms=term.code).count(),
        "invoices": Invoice.objects.filter(terms=term.code).count(),
        "bills": Bill.objects.filter(terms=term.code).count(),
    }
    return {k: v for k, v in counts.items() if v}


def make_code(name: str) -> str:
    """A unique code from a term's name, e.g. "Net 10" -> NET_10."""
    import re
    base = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:16] or "TERM"
    code, n = base, 2
    while PaymentTerm.objects.filter(code=code).exists():
        code, n = f"{base}_{n}", n + 1
    return code


DEFAULT_TERMS = [
    # code, name, days, end_of_month, months_after
    ("DUE_ON_RECEIPT", "Due on receipt", 0, False, 0),
    ("NET_15", "Net 15", 15, False, 0),
    ("NET_30", "Net 30", 30, False, 0),
    ("NET_45", "Net 45", 45, False, 0),
    ("NET_60", "Net 60", 60, False, 0),
    ("DUE_EOM", "Due end of the month", 0, True, 0),
    ("DUE_EOM_NEXT", "Due end of next month", 0, True, 1),
]


def ensure_default_terms(**kwargs):
    """Create any starter term that is missing (post_migrate). Terms can't be deleted, so this never undoes an edit.
    It also restores them after a test database flush."""
    for code, name, days, eom, months in DEFAULT_TERMS:
        PaymentTerm.objects.get_or_create(
            code=code, defaults={"name": name, "days": days, "end_of_month": eom, "months_after": months})

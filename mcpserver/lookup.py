"""Find records by the numbers and names people see, with "Did you mean" suggestions."""
import difflib
from datetime import date

from contacts.models import Customer, Vendor
from items.models import Item
from ledger.models import Account

from .runtime import Refused


def _suggest(word: str, kind: str, value: str, choices: list[str]) -> Refused:
    close = difflib.get_close_matches(value, choices, n=3, cutoff=0.6)
    if not close:
        lowered = value.lower()
        close = [c for c in choices if lowered in c.lower()][:3]
    hint = f" Did you mean {' or '.join(close)}?" if close else ""
    return Refused(f"No {kind} {word} {value}.{hint}")


def customer(name: str) -> Customer:
    name = (name or "").strip()
    found = Customer.objects.filter(name__iexact=name).first()
    if found is None:
        raise _suggest("named", "customer", name, list(Customer.objects.values_list("name", flat=True)))
    return found


def vendor(name: str) -> Vendor:
    name = (name or "").strip()
    found = Vendor.objects.filter(name__iexact=name).first()
    if found is None:
        raise _suggest("named", "vendor", name, list(Vendor.objects.values_list("name", flat=True)))
    return found


def item(sku: str) -> Item:
    sku = (sku or "").strip()
    found = Item.objects.filter(sku__iexact=sku).first()
    if found is None:
        raise _suggest("has SKU", "item", sku, list(Item.objects.values_list("sku", flat=True)))
    return found


def account(number_or_name: str) -> Account:
    """By number (1000) or exact name (Checking)."""
    value = (number_or_name or "").strip()
    found = Account.objects.filter(number=value).first() or Account.objects.filter(name__iexact=value).first()
    if found is None:
        choices = [f"{a.number} {a.name}" for a in Account.objects.filter(is_active=True)]
        raise _suggest("is", "account", value, choices)
    return found


def document(model, number: str, kind: str):
    number = (number or "").strip().upper()
    found = model.objects.filter(number__iexact=number).first()
    if found is None:
        raise Refused(f"No {kind} has the number {number}.")
    return found


def day(value: str | None, label: str = "date", default: date | None = None) -> date | None:
    if value is None or str(value).strip() == "":
        return default
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise Refused(f"Enter the {label} as YYYY-MM-DD, like 2026-11-04.") from None


def choice(value: str, choices, label: str) -> str:
    """Match a TextChoices value by value or label, case-insensitively."""
    v = (value or "").strip().lower()
    for key, text in choices.choices:
        if v in (key.lower(), text.lower()):
            return key
    options = ", ".join(text for _, text in choices.choices)
    raise Refused(f"Choose a {label}: {options}.")

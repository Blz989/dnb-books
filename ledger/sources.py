"""Plain names and links for the document behind a journal entry (source_type / source_id)."""
from django.urls import NoReverseMatch, reverse

_LABELS = {
    "sales.invoice": ("Invoice", "sales:invoice-detail"),
    "sales.payment": ("Customer payment", "sales:payment-detail"),
    "sales.deposit": ("Deposit", "sales:deposit-detail"),
    "sales.credit_note": ("Credit note", "sales:credit-detail"),
    "sales.refund": ("Refund", "sales:refund-detail"),
    "purchasing.bill": ("Bill", "purchasing:bill-detail"),
    "purchasing.payment": ("Vendor payment", "purchasing:payment-detail"),
    "purchasing.vendor_credit": ("Vendor credit", "purchasing:credit-detail"),
    "items.movement": ("Stock movement", None),
    "items.import": ("Opening stock", None),
    "items.void": ("Stock movement void", None),
    "items.cost_revision": ("Cost correction", None),
    "banking.transfer": ("Transfer", None),
    "banking.entry": ("Bank entry", None),
}


def describe(entry) -> dict | None:
    """{"label", "number", "url"} for the entry's source document, or None for a manual entry."""
    if not entry.source_type:
        return None
    if entry.source_type.startswith("migration."):
        return {"label": "Zoho import", "number": entry.source_id, "url": None}
    label, url_name = _LABELS.get(entry.source_type, (entry.source_type, None))
    url = None
    if url_name and entry.source_id:
        try:
            url = reverse(url_name, args=[entry.source_id])
        except NoReverseMatch:
            url = None
    return {"label": label, "number": entry.source_id, "url": url}

"""Lets other apps hear about invoice changes without sales importing them."""
from collections.abc import Callable

_invoice_changed: list[Callable] = []


def register_invoice_changed(fn: Callable) -> None:
    if fn not in _invoice_changed:
        _invoice_changed.append(fn)


def invoice_changed(invoice) -> None:
    """Called after an invoice is created, edited, or voided, inside the same transaction."""
    for fn in _invoice_changed:
        fn(invoice)

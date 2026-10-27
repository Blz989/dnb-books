"""Document numbers. A number is taken inside the saving transaction, so a failed save gives it back."""
from django.core.exceptions import PermissionDenied
from django.db import connection

from core.roles import require_admin

from .errors import SalesError
from .models import (
    CreditNote, Deposit, DocType, DocumentSequence, Invoice, Payment, Refund, SalesOrder,
)

DEFAULTS = {
    DocType.SO: ("SO-", 5),
    DocType.INV: ("INV-", 6),
    DocType.PMT: ("PMT-", 5),
    DocType.CN: ("CN-", 5),
    DocType.DEP: ("DEP-", 5),
    DocType.REF: ("REF-", 5),
}
MODELS = {
    DocType.SO: SalesOrder, DocType.INV: Invoice, DocType.PMT: Payment,
    DocType.CN: CreditNote, DocType.DEP: Deposit, DocType.REF: Refund,
}


def _sequence(doc_type: DocType) -> DocumentSequence:
    prefix, padding = DEFAULTS[DocType(doc_type)]
    seq, _ = DocumentSequence.objects.get_or_create(
        doc_type=doc_type, defaults={"prefix": prefix, "padding": padding, "next_number": 1}
    )
    return seq


def _format(seq: DocumentSequence, n: int) -> str:
    return f"{seq.prefix}{n:0{seq.padding}d}"


def _used_numbers(doc_type: DocType, prefix: str) -> list[int]:
    used = []
    for number in MODELS[DocType(doc_type)].objects.filter(number__startswith=prefix).values_list("number", flat=True):
        tail = number[len(prefix):]
        if tail.isdigit():
            used.append(int(tail))
    return used


def next_number(doc_type: DocType) -> str:
    """Call inside the caller's transaction.atomic(). Reads and increments the sequence."""
    if not connection.in_atomic_block:
        raise RuntimeError("next_number must be called inside transaction.atomic().")
    seq = _sequence(doc_type)
    model = MODELS[DocType(doc_type)]
    n = seq.next_number
    while model.objects.filter(number=_format(seq, n)).exists():
        n += 1
    seq.next_number = n + 1
    seq.save()
    return _format(seq, n)


def set_sequence(doc_type: DocType, *, prefix: str, next_number: int, user) -> DocumentSequence:
    require_admin(user)
    prefix = (prefix or "").strip()
    seq = _sequence(doc_type)
    if next_number < 1:
        raise SalesError("Enter a next number of 1 or more.")
    produced = f"{prefix}{next_number:0{seq.padding}d}"
    used = _used_numbers(doc_type, prefix)
    if MODELS[DocType(doc_type)].objects.filter(number=produced).exists():
        raise SalesError(f"{produced} is already used. Choose a higher next number.")
    clash = max((u for u in used if u >= next_number), default=None)
    if clash is not None:
        highest = f"{prefix}{clash:0{seq.padding}d}"
        raise SalesError(f"{highest} is already used. Choose a higher next number.")
    seq.prefix = prefix
    seq.next_number = next_number
    seq.save()
    return seq

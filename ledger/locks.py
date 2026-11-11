"""Registries that let other apps veto a void or react to one without ledger importing them."""
from collections.abc import Callable

from .models import JournalEntry

_lock_checks: list[Callable[[JournalEntry], str | None]] = []
_void_hooks: list[Callable[[JournalEntry], None]] = []
_document_sources: dict[str, str] = {}


def register_lock_check(fn: Callable[[JournalEntry], str | None]) -> None:
    if fn not in _lock_checks:
        _lock_checks.append(fn)


def register_void_hook(fn: Callable[[JournalEntry], None]) -> None:
    if fn not in _void_hooks:
        _void_hooks.append(fn)


def lock_message(entry: JournalEntry) -> str | None:
    """The first message from a registered check, or None when the entry is free."""
    for check in _lock_checks:
        message = check(entry)
        if message:
            return message
    return None


def run_void_hooks(entry: JournalEntry) -> None:
    for hook in _void_hooks:
        hook(entry)


def register_document_source(source_type: str, noun: str) -> None:
    """Entries (and stock movements) with this source_type belong to a document, such as an invoice. They are
    voided only by the document's own service."""
    _document_sources[source_type] = noun


def document_label(source_type: str, source_id: str) -> str | None:
    """"invoice INV-000123" for a document's entry or movement, None for anything else."""
    noun = _document_sources.get(source_type)
    return f"{noun} {source_id}" if noun else None


def document_noun(source_type: str) -> str | None:
    return _document_sources.get(source_type)


def document_lock_message(entry: JournalEntry) -> str | None:
    noun = _document_sources.get(entry.source_type)
    if noun is None:
        return None
    return f"This entry belongs to {noun} {entry.source_id}. Void the {noun} instead."


def void_block_message(entry: JournalEntry) -> str | None:
    """Why the Void button must not be offered for this entry, if it must not."""
    return lock_message(entry) or document_lock_message(entry)

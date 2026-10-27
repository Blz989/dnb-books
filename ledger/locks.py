"""Registries that let other apps veto a void or react to one without ledger importing them."""
from collections.abc import Callable

from .models import JournalEntry

_lock_checks: list[Callable[[JournalEntry], str | None]] = []
_void_hooks: list[Callable[[JournalEntry], None]] = []


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

from datetime import date
from decimal import Decimal

import pytest

from ledger import locks
from ledger.errors import EntryStateError
from ledger.models import EntryStatus, JournalEntry
from ledger.posting import LineSpec, post_entry, void_entry

D = Decimal


@pytest.fixture(autouse=True)
def clean_registries():
    checks, hooks = list(locks._lock_checks), list(locks._void_hooks)
    yield
    locks._lock_checks[:] = checks
    locks._void_hooks[:] = hooks


@pytest.fixture
def entry(admin_user, cash, revenue):
    return post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
        LineSpec(cash, debit=D("10.00")), LineSpec(revenue, credit=D("10.00"))])


def test_lock_check_blocks_void_and_posts_nothing(admin_user, entry):
    locks.register_lock_check(lambda e: "Locked for testing.")
    with pytest.raises(EntryStateError, match="Locked for testing."):
        void_entry(entry, user=admin_user, reason="oops")
    entry.refresh_from_db()
    assert entry.status == EntryStatus.POSTED
    assert JournalEntry.objects.count() == 1


def test_lock_check_applies_to_system_voids(admin_user, entry):
    locks.register_lock_check(lambda e: "Locked for testing.")
    with pytest.raises(EntryStateError, match="Locked for testing."):
        void_entry(entry, user=admin_user, reason="oops", system=True)


def test_first_non_none_message_wins(entry):
    locks.register_lock_check(lambda e: None)
    locks.register_lock_check(lambda e: "second")
    locks.register_lock_check(lambda e: "third")
    assert locks.lock_message(entry) == "second"


def test_void_hook_called_once_with_original_entry(admin_user, entry):
    seen = []
    locks.register_void_hook(seen.append)
    reversal = void_entry(entry, user=admin_user, reason="oops")
    assert [e.pk for e in seen] == [entry.pk]
    assert seen[0].status == EntryStatus.VOID
    assert reversal.reverses_id == entry.pk


def test_hook_not_called_when_locked(admin_user, entry):
    seen = []
    locks.register_lock_check(lambda e: "no")
    locks.register_void_hook(seen.append)
    with pytest.raises(EntryStateError):
        void_entry(entry, user=admin_user, reason="oops")
    assert seen == []


def test_no_registrations_changes_nothing(admin_user, entry):
    locks._lock_checks.clear()
    locks._void_hooks.clear()
    assert locks.lock_message(entry) is None
    void_entry(entry, user=admin_user, reason="oops")
    entry.refresh_from_db()
    assert entry.status == EntryStatus.VOID

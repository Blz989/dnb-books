from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError
from django.db.models import ProtectedError

from ledger.errors import ImmutableEntryError
from ledger.models import (
    Account, AccountSubtype, AccountType, EntryStatus, JournalEntry, JournalLine,
)


def test_subtype_must_match_type(db):
    with pytest.raises(ValidationError):
        Account.objects.create(number="1000", name="X", type=AccountType.ASSET, subtype=AccountSubtype.ACCOUNTS_PAYABLE)


def test_line_cannot_have_both_debit_and_credit(draft_entry, cash):
    with pytest.raises(IntegrityError):
        JournalLine.objects.create(entry=draft_entry, account=cash, debit=Decimal("1.00"), credit=Decimal("1.00"))


def test_posted_entry_model_save_blocked(posted_entry):
    posted_entry.memo = "changed"
    with pytest.raises(ImmutableEntryError, match="Posted entries can't be changed"):
        posted_entry.save()


def test_posted_entry_model_delete_blocked(posted_entry):
    with pytest.raises(ImmutableEntryError):
        posted_entry.delete()


def test_posted_line_model_save_blocked(posted_entry):
    line = posted_entry.lines.first()
    line.description = "changed"
    with pytest.raises(ImmutableEntryError):
        line.save()


def test_posted_entry_sql_update_blocked(posted_entry):
    with pytest.raises(DatabaseError, match="immutable"):
        JournalEntry.objects.filter(pk=posted_entry.pk).update(memo="changed")


def test_posted_entry_sql_delete_blocked(posted_entry):
    with pytest.raises(DatabaseError, match="immutable"):
        JournalEntry.objects.filter(pk=posted_entry.pk).delete()


def test_posted_line_sql_update_blocked(posted_entry):
    with pytest.raises(DatabaseError):
        JournalLine.objects.filter(entry=posted_entry).update(debit=Decimal("9.99"))


def test_line_cannot_be_added_to_posted_entry_in_sql(posted_entry, cash):
    with pytest.raises(DatabaseError):
        JournalLine.objects.bulk_create([JournalLine(entry=posted_entry, account=cash, debit=Decimal("1.00"))])


def test_posted_to_void_is_allowed_and_void_is_final(posted_entry):
    posted_entry.status = EntryStatus.VOID
    posted_entry.save()
    posted_entry.status = EntryStatus.POSTED
    with pytest.raises(ImmutableEntryError):
        posted_entry.save()
    with pytest.raises(DatabaseError, match="immutable"):
        JournalEntry.objects.filter(pk=posted_entry.pk).update(status=EntryStatus.POSTED)


def test_draft_can_be_edited_and_deleted(draft_entry):
    draft_entry.memo = "edited"
    draft_entry.save()
    draft_entry.delete()
    assert not JournalEntry.objects.filter(pk=draft_entry.pk).exists()


def test_account_with_lines_cannot_be_deleted(posted_entry, cash):
    with pytest.raises(ProtectedError):
        cash.delete()


def test_in_ledger_excludes_drafts(draft_entry, posted_entry):
    assert set(JournalLine.objects.in_ledger().values_list("entry_id", flat=True)) == {posted_entry.pk}


def test_display_number(draft_entry):
    assert draft_entry.display_number == f"JE-{draft_entry.pk:06d}"

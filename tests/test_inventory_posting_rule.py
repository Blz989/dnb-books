from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from ledger.errors import EntryStateError, InvalidLineError
from ledger.models import EntryStatus, JournalEntry, JournalLine
from ledger.posting import LineSpec, post_draft, post_entry, void_entry

D = Decimal
MESSAGE = "Line 1: inventory accounts can only change through stock movements."


def lines(inventory_account, cash):
    return [LineSpec(inventory_account, debit=D("10.00")), LineSpec(cash, credit=D("10.00"))]


@pytest.fixture
def items_entry(admin_user, inventory_account, cash):
    return post_entry(date=date(2026, 1, 5), user=admin_user, source_type="items.movement", source_id="1",
                      lines=lines(inventory_account, cash))


@pytest.fixture
def inventory_draft(admin_user, inventory_account, cash):
    e = JournalEntry.objects.create(date=date(2026, 1, 5), memo="Manual", created_by=admin_user)
    JournalLine.objects.create(entry=e, account=inventory_account, debit=D("10.00"))
    JournalLine.objects.create(entry=e, account=cash, credit=D("10.00"))
    return e


def test_manual_entry_to_inventory_rejected(admin_user, inventory_account, cash):
    with pytest.raises(InvalidLineError, match=MESSAGE):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=lines(inventory_account, cash))
    assert JournalEntry.objects.count() == 0


def test_credit_side_line_numbered_correctly(admin_user, inventory_account, cash):
    with pytest.raises(InvalidLineError, match="Line 2: inventory accounts"):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("10.00")), LineSpec(inventory_account, credit=D("10.00"))])


def test_items_source_may_post_to_inventory(items_entry):
    assert items_entry.status == EntryStatus.POSTED


def test_other_source_types_still_rejected(admin_user, inventory_account, cash):
    with pytest.raises(InvalidLineError, match=MESSAGE):
        post_entry(date=date(2026, 1, 5), user=admin_user, source_type="sales.invoice",
                   lines=lines(inventory_account, cash))


def test_post_draft_rejects_inventory_lines(admin_user, inventory_draft):
    with pytest.raises(InvalidLineError, match=MESSAGE):
        post_draft(inventory_draft, admin_user)
    inventory_draft.refresh_from_db()
    assert inventory_draft.status == EntryStatus.DRAFT


def test_void_entry_refuses_items_entries(admin_user, items_entry):
    with pytest.raises(EntryStateError, match="This entry was created by a stock movement. Void the movement instead."):
        void_entry(items_entry, user=admin_user, reason="oops")
    items_entry.refresh_from_db()
    assert items_entry.status == EntryStatus.POSTED


def test_void_entry_allowed_with_flag_for_items_service(admin_user, items_entry):
    reversal = void_entry(items_entry, user=admin_user, reason="stock void", allow_items_source=True)
    assert reversal.reverses_id == items_entry.pk
    items_entry.refresh_from_db()
    assert items_entry.status == EntryStatus.VOID


def test_admin_post_action_shows_inventory_message(client, admin_user, inventory_draft):
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:ledger_journalentry_changelist"),
        {"action": "post_drafts", "_selected_action": [inventory_draft.pk]}, follow=True)
    assert MESSAGE.encode() in r.content
    inventory_draft.refresh_from_db()
    assert inventory_draft.status == EntryStatus.DRAFT

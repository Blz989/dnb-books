from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from ledger.models import (
    Account, AccountingPeriod, EntryStatus, JournalEntry, JournalLine,
)
from ledger.periods import close_period

D = Decimal


@pytest.fixture
def logged_in(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def unbalanced_draft(admin_user, cash, revenue):
    e = JournalEntry.objects.create(date=date(2026, 1, 15), memo="Bad", created_by=admin_user)
    JournalLine.objects.create(entry=e, account=cash, debit=D("100.00"))
    JournalLine.objects.create(entry=e, account=revenue, credit=D("90.00"))
    return e


def post_action(client, entries):
    return client.post(
        reverse("admin:ledger_journalentry_changelist"),
        {"action": "post_drafts", "_selected_action": [e.pk for e in entries]},
        follow=True,
    )


def line_formset(rows):
    data = {
        "lines-TOTAL_FORMS": str(len(rows)), "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
    }
    for i, (acct, debit, credit) in enumerate(rows):
        data[f"lines-{i}-account"] = str(acct.pk)
        data[f"lines-{i}-debit"] = debit
        data[f"lines-{i}-credit"] = credit
        data[f"lines-{i}-description"] = ""
    return data


def test_admin_post_action_shows_plain_error(logged_in, unbalanced_draft):
    r = post_action(logged_in, [unbalanced_draft])
    assert b"Debits (100.00) and credits (90.00) must be equal. Difference: 10.00." in r.content
    unbalanced_draft.refresh_from_db()
    assert unbalanced_draft.status == EntryStatus.DRAFT


def test_admin_post_action_posts_balanced_draft(logged_in, draft_entry, admin_user):
    post_action(logged_in, [draft_entry])
    draft_entry.refresh_from_db()
    assert draft_entry.status == EntryStatus.POSTED and draft_entry.posted_by == admin_user


def test_admin_creates_draft_with_lines(logged_in, admin_user, cash, revenue):
    data = {"date": "2026-02-01", "reference": "R1", "memo": "Manual"}
    data.update(line_formset([(cash, "25.00", "0"), (revenue, "0", "25.00")]))
    r = logged_in.post(reverse("admin:ledger_journalentry_add"), data)
    assert r.status_code == 302
    e = JournalEntry.objects.get()
    assert e.status == EntryStatus.DRAFT and e.created_by == admin_user and e.lines.count() == 2


def test_admin_line_with_both_sides_gets_plain_message(logged_in, cash, revenue):
    data = {"date": "2026-02-01", "memo": "Manual"}
    data.update(line_formset([(cash, "25.00", "5.00"), (revenue, "0", "25.00")]))
    r = logged_in.post(reverse("admin:ledger_journalentry_add"), data)
    assert r.status_code == 200 and b"Enter a debit or a credit, not both." in r.content
    assert JournalEntry.objects.count() == 0


def test_admin_cannot_set_status_posted_from_form(logged_in, draft_entry):
    data = {"date": "2026-01-15", "memo": "x", "status": "POSTED"}
    data.update({
        "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
    })
    logged_in.post(reverse("admin:ledger_journalentry_change", args=[draft_entry.pk]), data)
    draft_entry.refresh_from_db()
    assert draft_entry.status == EntryStatus.DRAFT


def test_posted_entry_change_view_is_read_only(logged_in, posted_entry):  # Review Focus 5
    url = reverse("admin:ledger_journalentry_change", args=[posted_entry.pk])
    assert logged_in.get(url).status_code == 200
    r = logged_in.post(url, {"date": "2026-01-15", "memo": "hacked"})
    assert r.status_code == 403
    posted_entry.refresh_from_db()
    assert posted_entry.memo == "Posted"


def test_posted_entry_cannot_be_deleted_in_admin(logged_in, posted_entry):
    url = reverse("admin:ledger_journalentry_delete", args=[posted_entry.pk])
    assert logged_in.post(url, {"post": "yes"}).status_code == 403
    assert JournalEntry.objects.filter(pk=posted_entry.pk).exists()


def test_no_bulk_delete_action_for_entries(logged_in, draft_entry):
    r = logged_in.post(
        reverse("admin:ledger_journalentry_changelist"),
        {"action": "delete_selected", "_selected_action": [draft_entry.pk], "post": "yes"},
        follow=True,
    )
    assert JournalEntry.objects.filter(pk=draft_entry.pk).exists()
    assert r.status_code == 200


def test_standard_user_has_no_void_button(client, standard_user, posted_entry):
    client.force_login(standard_user)
    r = client.get(reverse("admin:ledger_journalentry_change", args=[posted_entry.pk]))
    assert r.status_code == 200 and b"/void/" not in r.content


def test_admin_sees_void_button(logged_in, posted_entry):
    r = logged_in.get(reverse("admin:ledger_journalentry_change", args=[posted_entry.pk]))
    assert f"/admin/ledger/journalentry/{posted_entry.pk}/void/".encode() in r.content


def test_standard_user_cannot_open_void_form(client, standard_user, posted_entry):
    client.force_login(standard_user)
    assert client.get(reverse("admin:ledger_journalentry_void", args=[posted_entry.pk])).status_code == 403


def test_void_form_voids_entry(logged_in, posted_entry):
    url = reverse("admin:ledger_journalentry_void", args=[posted_entry.pk])
    r = logged_in.post(url, {"date": "2026-01-15", "reason": "Duplicate"})
    assert r.status_code == 302
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.VOID and posted_entry.reversals.count() == 1


def test_void_in_closed_month_shows_plain_error(logged_in, admin_user, posted_entry):  # Review Focus 3
    close_period(2026, 1, admin_user)
    url = reverse("admin:ledger_journalentry_void", args=[posted_entry.pk])
    r = logged_in.post(url, {"date": "2026-01-15", "reason": "Oops"})
    assert r.status_code == 200 and b"January 2026 is closed" in r.content
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.POSTED
    r = logged_in.post(url, {"date": "2026-02-01", "reason": "Oops"})
    assert r.status_code == 302


def test_void_requires_reason(logged_in, posted_entry):
    url = reverse("admin:ledger_journalentry_void", args=[posted_entry.pk])
    r = logged_in.post(url, {"date": "2026-01-15", "reason": ""})
    assert r.status_code == 200
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.POSTED


def test_close_periods_action_admin_only(client, admin_user, standard_user, db):
    p = AccountingPeriod.objects.create(year=2026, month=3)
    url = reverse("admin:ledger_accountingperiod_changelist")
    payload = {"action": "close_periods", "_selected_action": [p.pk]}
    client.force_login(standard_user)
    client.post(url, payload, follow=True)
    p.refresh_from_db()
    assert not p.is_closed
    client.force_login(admin_user)
    client.post(url, payload, follow=True)
    p.refresh_from_db()
    assert p.is_closed and p.closed_by == admin_user


def test_reopen_without_reason_rejected(logged_in, admin_user, db):
    close_period(2026, 3, admin_user)
    p = AccountingPeriod.objects.get(year=2026, month=3)
    url = reverse("admin:ledger_accountingperiod_reopen", args=[p.pk])
    r = logged_in.post(url, {"reason": "   "})
    assert r.status_code == 200
    p.refresh_from_db()
    assert p.is_closed


def test_reopen_with_reason(logged_in, admin_user, db):
    close_period(2026, 3, admin_user)
    p = AccountingPeriod.objects.get(year=2026, month=3)
    url = reverse("admin:ledger_accountingperiod_reopen", args=[p.pk])
    assert logged_in.post(url, {"reason": "Late bill"}).status_code == 302
    p.refresh_from_db()
    assert not p.is_closed and p.reopen_reason == "Late bill"


def test_standard_user_cannot_reopen(client, standard_user, admin_user, db):
    close_period(2026, 3, admin_user)
    p = AccountingPeriod.objects.get(year=2026, month=3)
    client.force_login(standard_user)
    assert client.post(
        reverse("admin:ledger_accountingperiod_reopen", args=[p.pk]), {"reason": "x"}
    ).status_code == 403


def test_account_with_lines_delete_page_has_no_500(logged_in, posted_entry, cash):
    r = logged_in.get(reverse("admin:ledger_account_delete", args=[cash.pk]))
    assert r.status_code == 200 and b"protected related objects" in r.content
    assert Account.objects.filter(pk=cash.pk).exists()

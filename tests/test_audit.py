from auditlog.models import LogEntry
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse

from ledger.models import AccountingPeriod, JournalEntry, JournalLine
from ledger.periods import close_period


def logs_for(model):
    return LogEntry.objects.filter(content_type=ContentType.objects.get_for_model(model))


def test_posting_is_audited_for_entry_and_lines(posted_entry):
    assert logs_for(JournalEntry).filter(object_pk=str(posted_entry.pk)).exists()
    assert logs_for(JournalLine).count() == 2


def test_void_status_change_is_audited(admin_user, posted_entry):
    from ledger.posting import void_entry

    void_entry(posted_entry, user=admin_user, reason="x")
    changes = [l.changes for l in logs_for(JournalEntry).filter(object_pk=str(posted_entry.pk))]
    assert any("status" in c and c["status"][1] == "VOID" for c in changes)


def test_period_close_is_audited(admin_user, db):
    close_period(2026, 3, admin_user)
    assert logs_for(AccountingPeriod).exists()


def test_admin_screen_changes_record_who(client, admin_user, draft_entry):
    client.force_login(admin_user)
    client.post(
        reverse("admin:ledger_journalentry_changelist"),
        {"action": "post_drafts", "_selected_action": [draft_entry.pk]},
    )
    log = logs_for(JournalEntry).filter(object_pk=str(draft_entry.pk)).order_by("-timestamp", "-id").first()
    assert log.actor == admin_user and log.timestamp is not None


def test_reopen_reason_is_audited_with_actor(client, admin_user, db):
    close_period(2026, 3, admin_user)
    p = AccountingPeriod.objects.get(year=2026, month=3)
    client.force_login(admin_user)
    client.post(reverse("admin:ledger_accountingperiod_reopen", args=[p.pk]), {"reason": "Late bill"})
    log = logs_for(AccountingPeriod).order_by("-id").first()
    assert log.actor == admin_user and "Late bill" in str(log.changes)

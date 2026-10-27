import logging
from datetime import date, datetime
from decimal import Decimal

import pytest

from banking import plaid_client, sync
from banking.models import (
    AccountKind, BankAccount, BankTransaction, ItemStatus, PlaidItem, TxnSource, TxnStatus,
)
from banking.plaid_client import PlaidClient, PlaidError
from tests.fake_plaid import FakePlaidClient

D = Decimal
TOKEN = "access-sandbox-fake-token"


@pytest.fixture
def item(db, field_key, checking, card_account):
    it = PlaidItem(institution_name="First Platypus Bank")
    it.set_access_token(TOKEN)
    it.save()
    checking.plaid_item, checking.plaid_account_id = it, "acct-chk"
    checking.save()
    card_account.plaid_item, card_account.plaid_account_id = it, "acct-card"
    card_account.save()
    return it


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(plaid_client, "get_client", lambda: client)
        return client

    return install


def test_two_pages_applied_and_cursor_saved(item, fake, checking, card_account):
    client = fake(FakePlaidClient())
    result = sync.sync_item(item)
    assert client.calls == [(TOKEN, None), (TOKEN, "cursor-1")]
    assert (result.added, result.skipped_pending) == (4, 1)
    item.refresh_from_db()
    assert item.cursor == "cursor-2" and item.status == ItemStatus.OK and item.last_sync_at is not None
    assert BankTransaction.objects.filter(bank_account=checking).count() == 3
    assert BankTransaction.objects.filter(bank_account=card_account).count() == 1
    assert not BankTransaction.objects.filter(external_id__in=["tx-4", "tx-6"]).exists()


def test_sign_is_flipped_to_account_point_of_view(item, fake, checking, card_account):
    fake(FakePlaidClient())
    sync.sync_item(item)
    assert BankTransaction.objects.get(external_id="tx-3").amount == D("-25.00")  # Plaid 25.00, money out
    assert BankTransaction.objects.get(external_id="tx-1").amount == D("1500.00")  # Plaid -1500.00, money in
    assert BankTransaction.objects.get(external_id="tx-5").amount == D("-86.40")  # card purchase
    t = BankTransaction.objects.get(external_id="tx-2")
    assert (t.check_number, t.source, t.date) == ("2050", TxnSource.PLAID, date(2026, 1, 5))


def test_second_sync_resumes_from_cursor_and_adds_nothing(item, fake):
    fake(FakePlaidClient())
    sync.sync_item(item)
    client = fake(FakePlaidClient(pages=("sync_changes.json",)))
    client._cursors = ["cursor-2"]
    result = sync.sync_item(item)
    assert client.calls == [(TOKEN, "cursor-2")]
    assert result.added == 0


def test_modified_updates_new_rows_only_and_removed_deletes_new(item, fake, checking):
    fake(FakePlaidClient())
    sync.sync_item(item)
    booked = BankTransaction.objects.get(external_id="tx-2")
    booked.status = TxnStatus.MATCHED
    booked.save()
    gone = BankTransaction.objects.get(external_id="tx-1")
    gone.status = TxnStatus.BOOKED
    gone.save()

    client = fake(FakePlaidClient(pages=("sync_changes.json",)))
    client._cursors = ["cursor-2"]
    result = sync.sync_item(item)

    coffee = BankTransaction.objects.filter(external_id="tx-3")
    assert not coffee.exists()  # modified then removed in the same page: removed wins
    booked.refresh_from_db()
    assert (booked.description, booked.date, booked.amount) == ("Check 2050", date(2026, 1, 5), D("-420.00"))
    gone.refresh_from_db()
    assert gone.exclude_reason == "Removed by the bank." and gone.status == TxnStatus.BOOKED
    assert result.removed == 2 and result.modified == 1


def test_modified_new_row_is_updated(item, fake):
    fake(FakePlaidClient())
    sync.sync_item(item)
    client = fake(FakePlaidClient(pages=("sync_changes.json",)))
    client._cursors = ["cursor-2"]
    client._payloads[0]["removed"] = []
    sync.sync_item(item)
    t = BankTransaction.objects.get(external_id="tx-3")
    assert (t.description, t.amount) == ("Coffee Shop #2", D("-27.50"))


def test_login_required_sets_needs_login(item, fake):
    fake(FakePlaidClient(error=PlaidError("ITEM_LOGIN_REQUIRED", "the login details of this item have changed")))
    sync.sync_item(item)
    item.refresh_from_db()
    assert item.status == ItemStatus.NEEDS_LOGIN
    assert item.last_error == "the login details of this item have changed"


def test_other_errors_set_error_with_plaid_message(item, fake):
    fake(FakePlaidClient(error=PlaidError("INTERNAL_SERVER_ERROR", "Plaid had a problem.")))
    sync.sync_item(item)
    item.refresh_from_db()
    assert item.status == ItemStatus.ERROR and item.last_error == "Plaid had a problem."


def test_failed_sync_keeps_old_cursor_and_rows(item, fake):
    fake(FakePlaidClient())
    sync.sync_item(item)
    fake(FakePlaidClient(error=PlaidError("INTERNAL_SERVER_ERROR", "down")))
    sync.sync_item(item)
    item.refresh_from_db()
    assert item.cursor == "cursor-2"
    assert BankTransaction.objects.count() == 4


def test_missing_field_key_shows_message(item, fake, settings):
    fake(FakePlaidClient())
    settings.DNB_FIELD_KEY = ""
    sync.sync_item(item)
    item.refresh_from_db()
    assert item.status == ItemStatus.ERROR
    assert item.last_error == "Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts."


def test_sync_all_continues_after_a_failing_item(item, fake, field_key):
    other = PlaidItem(institution_name="Second Bank")
    other.set_access_token("access-other")
    other.save()
    fake(FakePlaidClient(error=PlaidError("INTERNAL_SERVER_ERROR", "down")))
    sync.sync_all()
    assert set(PlaidItem.objects.values_list("status", flat=True)) == {ItemStatus.ERROR}


def test_token_never_in_error_text_or_logs(item, fake, caplog):
    fake(FakePlaidClient(error=PlaidError("INTERNAL_SERVER_ERROR", "Plaid had a problem.")))
    with caplog.at_level(logging.DEBUG):
        sync.sync_item(item)
    assert TOKEN not in caplog.text
    assert TOKEN not in str(PlaidError("X", "Plaid had a problem."))


def test_real_client_scrubs_secrets_from_errors():
    import plaid

    client = PlaidClient("client-id", "the-secret-value", "sandbox")
    exc = plaid.ApiException(status=400, reason="Bad")
    exc.body = '{"error_code": "INVALID_FIELD", "error_message": "bad token access-abc and the-secret-value"}'
    err = client._error(exc, "access-abc")
    assert err.code == "INVALID_FIELD"
    assert "access-abc" not in str(err) and "the-secret-value" not in str(err)


def test_real_client_wraps_non_api_errors():
    client = PlaidClient("client-id", "the-secret-value", "sandbox")
    err = client._error(ConnectionError("boom the-secret-value"))
    assert (err.code, str(err)) == ("CONNECTION_ERROR", "Could not reach Plaid.")


def test_item_without_mapped_account_skips_its_rows(item, fake, card_account):
    card_account.plaid_account_id = ""
    card_account.save()
    fake(FakePlaidClient())
    sync.sync_item(item)
    assert BankTransaction.objects.filter(bank_account=card_account).count() == 0


def test_scheduler_runs_sync_banks_at_0600(settings):
    from backup.management.commands.run_scheduler import upcoming_jobs

    settings.DNB_BANK_SYNC_TIMES = "06:00,10:00,14:00,18:00"
    jobs = upcoming_jobs(datetime(2026, 3, 1, 0, 0))
    syncs = sorted(when for when, command in jobs if command == "sync_banks")
    assert [w.strftime("%H:%M") for w in syncs] == ["06:00", "10:00", "14:00", "18:00"]
    assert {c for _, c in jobs} == {"backup_now", "restore_drill", "sync_banks"}


def test_sandbox_check_refuses_outside_sandbox(settings):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.PLAID_ENV = "production"
    with pytest.raises(CommandError, match="only runs with PLAID_ENV=sandbox"):
        call_command("plaid_sandbox_check")


def test_every_page_is_fetched_before_anything_is_written(item, monkeypatch):
    """SQLite holds the write lock for a whole transaction, so slow bank calls must not happen inside one."""
    rows_seen = []

    class Watching(FakePlaidClient):
        def transactions_sync(self, access_token, cursor):
            rows_seen.append(BankTransaction.objects.count())
            return super().transactions_sync(access_token, cursor)

    monkeypatch.setattr(plaid_client, "get_client", lambda: Watching())
    sync.sync_item(item)
    assert rows_seen == [0, 0]
    assert BankTransaction.objects.count() == 4

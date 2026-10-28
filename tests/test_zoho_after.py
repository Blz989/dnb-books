from datetime import date
from decimal import Decimal

import pytest

from banking.models import Reconciliation, ReconStatus
from banking.reconcile import ReconcileError, difference, finish, set_statement_balance
from ledger.models import JournalLine
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from sales.models import DocType
from sales.numbering import next_number
from tests.zoho_sample import BANK, build_sample_export


@pytest.fixture
def replay(db, admin_user, tmp_path):
    r = Replay(read_export(build_sample_export(tmp_path)), admin_user)
    r.load_master()
    for event in r.events():
        r.post(event)
    return r


@pytest.fixture
def after(replay):
    return replay.finish()


def _bank_rec():
    return Reconciliation.objects.get(bank_account__ledger_account__name=BANK)


def test_dec31_reconciliation_created_in_progress_all_ticked_blank_balance(replay, after):
    rec = _bank_rec()
    assert (rec.statement_date, rec.statement_balance, rec.status) == (
        date(2025, 12, 31), None, ReconStatus.IN_PROGRESS)
    lines = JournalLine.objects.in_ledger().filter(
        account=rec.bank_account.ledger_account, entry__date__lte=date(2025, 12, 31))
    assert lines.count() > 0
    assert rec.items.count() == lines.count() and not rec.items.filter(cleared=False).exists()
    assert set(rec.items.values_list("journal_line_id", flat=True)) == set(lines.values_list("pk", flat=True))
    assert difference(rec) is None
    # accounts with no lines through Dec 31 get none
    assert Reconciliation.objects.count() <= len(replay.bank_accounts)


def test_finish_requires_statement_balance(replay, after, admin_user):
    with pytest.raises(ReconcileError, match=r"^Enter the statement balance\.$"):
        finish(_bank_rec(), user=admin_user)
    assert _bank_rec().status == ReconStatus.IN_PROGRESS


def test_set_statement_balance_then_finish_at_zero(replay, after, admin_user):
    rec = _bank_rec()
    from banking.reconcile import cleared_total
    set_statement_balance(rec, cleared_total(rec), admin_user)
    assert difference(_bank_rec()) == Decimal("0.00")
    finish(rec, user=admin_user)
    assert _bank_rec().status == ReconStatus.FINISHED
    with pytest.raises(ReconcileError):
        set_statement_balance(rec, Decimal("1.00"), admin_user)


def test_sequences_continue_after_zoho_numbers(replay, after):
    from django.db import transaction
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000010"
        assert next_number(DocType.SO) == "SO-00009"
        assert next_number(DocType.CN) == "CN-00002"
        assert next_number(DocType.PO) == "PO-00004"
        assert next_number(DocType.PMT) == "PMT-00001"


def test_unbilled_orders_listed(replay, after):
    numbers = [o.number for o, *_ in after.unbilled_orders]
    # SO-0001/0002 predate 2024-03-29; SO-0006 has a Cordon bill; SO-0007's bill is not Cordon's.
    assert numbers == ["SO-0003", "SO-0004", "SO-0005", "SO-0007", "SO-0008"]
    order, profit, at_50, at_55 = after.unbilled_orders[0]
    assert at_50 == max(Decimal("0.00"), (profit * Decimal("0.50")).quantize(Decimal("0.01")))
    assert at_55 >= at_50


def test_undeposited_by_year(replay, after):
    assert after.undeposited_by_year == {2024: (1, Decimal("1012.50")), 2025: (1, Decimal("295.00"))}


def test_existing_reconciliations_unaffected():
    # The Phase 5a behavior is covered by tests/test_reconcile.py and tests/test_reconcile_views.py.
    from banking import reconcile
    assert reconcile.ZERO == Decimal("0.00")


def test_working_page_blank_balance_then_saved(replay, after, admin_user, client):
    rec = _bank_rec()
    client.force_login(admin_user)
    url = f"/banking/reconcile/{rec.bank_account_id}/{rec.pk}/"
    page = client.get(url).content.decode()
    assert "&mdash;" in page and 'id="statement-balance"' in page
    client.post(url + "balance/", {"statement_balance": "1,234.50"})
    assert _bank_rec().statement_balance == Decimal("1234.50")
    client.post(url + "balance/", {"statement_balance": "abc"})
    assert _bank_rec().statement_balance == Decimal("1234.50")

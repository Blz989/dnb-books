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


def test_existing_reconciliations_unaffected(db, admin_user, tmp_path):
    from banking import reconcile
    from banking.models import AccountKind, BankAccount
    from ledger.models import Account, AccountSubtype, AccountType
    from ledger.posting import LineSpec, post_entry

    cash = Account.objects.create(number="1000", name="Checking", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    income = Account.objects.create(number="4000", name="Sales", type=AccountType.INCOME, subtype=AccountSubtype.INCOME)
    bank = BankAccount.objects.create(ledger_account=cash, kind=AccountKind.CHECKING)
    post_entry(date=date(2026, 1, 3), memo="Deposit", user=admin_user, lines=[
        LineSpec(account=cash, debit=Decimal("100.00")), LineSpec(account=income, credit=Decimal("100.00"))])
    rec = reconcile.start(bank, statement_date=date(2026, 1, 31), statement_balance=Decimal("90.00"), user=admin_user)
    for item in rec.items.all():
        reconcile.toggle(item, True)
    diff = reconcile.difference(rec)
    assert isinstance(diff, Decimal) and diff == Decimal("-10.00")
    with pytest.raises(ReconcileError, match="The difference must be 0.00 to finish."):
        reconcile.finish(rec, user=admin_user)
    reconcile.set_statement_balance(rec, Decimal("100.00"), admin_user)
    assert reconcile.difference(rec) == Decimal("0.00")
    reconcile.finish(rec, user=admin_user)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED and rec.statement_balance == Decimal("100.00")


def _extra_replay(admin_user, tmp_path, extra):
    r = Replay(read_export(build_sample_export(tmp_path, extra_rows=extra)), admin_user)
    r.load_master()
    for event in r.events():
        r.post(event)
    return r


def test_unbilled_profit_split_exact_and_floored(db, admin_user, tmp_path):
    from tests.zoho_sample import _invoice, _sales_order
    # Item A costs 12.00 a unit (FIFO). SO-0009 sells one at 12.30: profit 0.30, so 55% is 0.165 -> 0.17
    # (half up) and 50% is 0.15. SO-0010 sells one at 10.00: profit -2.00, handling floored at 0.00.
    extra = {
        "sales_orders": (
            _sales_order("50000000009", "SO-0009", "2025-07-01", "Closed", "Acme Hardware", [("A", 1, Decimal("12.30"))])
            + _sales_order("50000000010", "SO-0010", "2025-07-01", "Closed", "Acme Hardware", [("A", 1, Decimal("10.00"))])),
        "invoices": (
            _invoice("51000000010", "INV-000010", "2025-07-02", "Open", "Acme Hardware", "SO-0009",
                     [("A", 1, Decimal("12.30"))])
            + _invoice("51000000011", "INV-000011", "2025-07-02", "Open", "Acme Hardware", "SO-0010",
                       [("A", 1, Decimal("10.00"))])),
    }
    after = _extra_replay(admin_user, tmp_path, extra).finish()
    rows = {o.number: (profit, a50, a55) for o, profit, a50, a55 in after.unbilled_orders}
    assert rows["SO-0003"] == (Decimal("500.00"), Decimal("250.00"), Decimal("275.00"))  # 20 x (50.00 - 25.00)
    assert rows["SO-0009"] == (Decimal("0.30"), Decimal("0.15"), Decimal("0.17"))
    assert rows["SO-0010"] == (Decimal("-2.00"), Decimal("0.00"), Decimal("0.00"))


def test_negative_items_listed_with_on_hand(db, admin_user, tmp_path):
    from tests.zoho_sample import _invoice
    # Gadget (GAD-C): 40 opening + 30 on BILL-002 - 30 sold (INV-000003, INV-000009) = 40 on hand; 60 more invoiced leaves -20.
    extra = {"invoices": _invoice("51000000012", "INV-000012", "2025-12-01", "Open", "Acme Hardware", "",
                                  [("C", 60, Decimal("50.00"))])}
    after = _extra_replay(admin_user, tmp_path, extra).finish()
    assert [(item.sku, qty) for item, qty in after.negative_items] == [("GAD-C", -20)]


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

"""repair_undeposited: fix an already imported database where old payments were never deposited."""
from datetime import date
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ledger.models import JournalEntry
from ledger.posting import LineSpec, post_entry
from sales.checks import undeposited_check
from sales.models import PaymentMethod, UndepositedClearing
from sales.posting.receipts import receive_payment

D = Decimal


@pytest.fixture
def backups(monkeypatch):
    calls = []
    monkeypatch.setattr("migration.management.commands.repair_undeposited.run_backup", lambda: calls.append(1))
    return calls


@pytest.fixture
def imported(customer, admin_user, sales_settings, bank, expense):
    """Two 2023 cash payments cleared by a journal, and a 2025 one paid out through an expense."""
    def pay(amount, when):
        return receive_payment(customer=customer, date=when, method=PaymentMethod.CASH, amount=D(amount), user=admin_user)

    old = [pay("100.00", date(2023, 5, 1)), pay("25.46", date(2023, 6, 1))]
    late = pay("50.00", date(2025, 2, 25))
    undeposited = sales_settings.undeposited_account
    journal = post_entry(
        date=date(2023, 12, 31), user=admin_user, memo="Move Undeposited to Bank ", source_type="migration.journal",
        source_id="J1", lines=[LineSpec(bank, debit=D("125.46")), LineSpec(undeposited, credit=D("125.46"))])
    paid_out = post_entry(
        date=date(2025, 3, 1), user=admin_user, memo="Vendor payment", source_type="migration.expense", source_id="E1",
        lines=[LineSpec(expense, debit=D("50.00")), LineSpec(undeposited, credit=D("50.00"))])
    return old, late, journal, paid_out


def run(*args):
    out = StringIO()
    try:
        call_command("repair_undeposited", *args, stdout=out)
    finally:
        pass
    return out.getvalue()


def test_dry_run_reports_and_changes_nothing(imported, backups):
    text = run()
    assert "2 payments, 125.46, cleared by JE-" in text
    assert "Dry run" in text and not UndepositedClearing.objects.exists() and backups == []
    assert undeposited_check() != []


def test_apply_backs_up_then_links_and_is_idempotent(imported, backups):
    old, late, journal, paid_out = imported
    entries = JournalEntry.objects.count()
    text = run("--apply")
    assert backups == [1]
    assert UndepositedClearing.objects.get().journal_entry == journal
    assert JournalEntry.objects.count() == entries  # nothing posted
    assert "still waiting" in text and late.number in text and paid_out.display_number in text
    again = run("--apply")
    assert "Nothing to link" in again and UndepositedClearing.objects.count() == 1


def test_pairs_file_links_the_rest(imported, backups, tmp_path):
    old, late, journal, paid_out = imported
    pairs = tmp_path / "pairs.csv"
    pairs.write_text(f"entry,payment_numbers\n{paid_out.display_number},{late.number}\n", encoding="utf-8")
    run("--apply", "--pairs", str(pairs))
    assert UndepositedClearing.objects.count() == 2
    assert undeposited_check() == []


def test_refuses_to_guess_when_a_total_is_off(imported, backups, customer, admin_user):
    receive_payment(customer=customer, date=date(2023, 7, 1), method=PaymentMethod.CASH, amount=D("0.01"), user=admin_user)
    with pytest.raises(CommandError, match="did not match"):
        run("--apply")
    assert not UndepositedClearing.objects.exists() and backups == []


def test_a_bad_pairs_row_stops_everything(imported, backups, tmp_path):
    old, late, journal, paid_out = imported
    pairs = tmp_path / "pairs.csv"
    pairs.write_text(f"entry,payment_numbers\n{paid_out.display_number},{old[0].number}\n", encoding="utf-8")
    with pytest.raises(CommandError):
        run("--apply", "--pairs", str(pairs))
    assert not UndepositedClearing.objects.exists()

from datetime import date
from decimal import Decimal

import pytest

from ledger.models import JournalEntry
from ledger.posting import LineSpec, post_entry, void_entry
from sales.checks import close_check, undeposited_check
from sales.errors import DepositError
from sales.models import PaymentMethod
from sales.posting.clearings import clear_with_entry
from sales.posting.deposits import make_deposit, undeposited_payments
from sales.posting.receipts import receive_payment

D = Decimal


def d(month, day, year=2026):
    return date(year, month, day)


@pytest.fixture
def old_payments(customer, admin_user, sales_settings):
    def pay(amount, day):
        return receive_payment(customer=customer, date=d(3, day), method=PaymentMethod.CASH, amount=D(amount), user=admin_user)

    return pay("100.00", 3), pay("40.50", 4)


def journal(admin_user, bank, sales_settings, amount="140.50", day=31):
    """What Zoho's manual "Move Undeposited to Bank" journal looked like: bank in, Undeposited out."""
    return post_entry(
        date=d(3, day), user=admin_user, memo="Move Undeposited to Bank", source_type="migration.journal", source_id="J1",
        lines=[LineSpec(bank, debit=D(amount)), LineSpec(sales_settings.undeposited_account, credit=D(amount))])


def test_clearing_takes_payments_off_the_undeposited_list_without_posting(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings)
    before = JournalEntry.objects.count()
    clearing = clear_with_entry(payments=old_payments, entry=entry, user=admin_user, note="Zoho journal 31 Mar")
    assert JournalEntry.objects.count() == before
    assert list(undeposited_payments()) == []
    assert {p.pk for p in clearing.payments.all()} == {p.pk for p in old_payments}
    assert undeposited_check() == [] and close_check(2026, 3) == []


def test_before_the_entry_date_they_still_count_as_undeposited(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings)
    clear_with_entry(payments=old_payments, entry=entry, user=admin_user)
    assert undeposited_check(d(3, 30)) == []  # payments 140.50 undeposited and the ledger has them too


def test_total_must_match_the_entry_exactly(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings, amount="140.51")
    with pytest.raises(DepositError, match="add up to 140.50 but the entry takes out 140.51"):
        clear_with_entry(payments=old_payments, entry=entry, user=admin_user)
    assert len(undeposited_payments()) == 2


def test_an_entry_clears_only_once(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings)
    clear_with_entry(payments=old_payments, entry=entry, user=admin_user)
    with pytest.raises(DepositError, match="already accounts for"):
        clear_with_entry(payments=old_payments, entry=entry, user=admin_user)


def test_payment_dated_after_the_entry_is_refused(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings, day=3)
    with pytest.raises(DepositError, match="dated after"):
        clear_with_entry(payments=old_payments, entry=entry, user=admin_user)


def test_a_deposited_payment_is_refused(old_payments, bank, admin_user, sales_settings):
    make_deposit(payments=old_payments[:1], bank_account=bank, date=d(3, 5), user=admin_user)
    entry = journal(admin_user, bank, sales_settings, amount="40.50")
    with pytest.raises(DepositError, match="is already in deposit"):
        clear_with_entry(payments=old_payments[:1], entry=entry, user=admin_user)


def test_voiding_the_entry_puts_the_payments_back(old_payments, bank, admin_user, sales_settings):
    entry = journal(admin_user, bank, sales_settings)
    clear_with_entry(payments=old_payments, entry=entry, user=admin_user)
    void_entry(entry, user=admin_user, reason="Mistake", date=d(4, 2))
    assert len(undeposited_payments()) == 2
    assert undeposited_check() == []

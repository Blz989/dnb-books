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


# The real case: two payments (626.00 + 250.00) plus a 590.00 "Draw" journal debit to Undeposited Funds were all
# paid out by one vendor payment that credits Undeposited Funds 1,466.00.
@pytest.fixture
def draw_case(customer, admin_user, sales_settings, bank, expense, equity):
    pays = [receive_payment(customer=customer, date=d(6, day), method=PaymentMethod.CASH, amount=D(a), user=admin_user)
            for day, a in ((14, "626.00"), (20, "250.00"))]
    und = sales_settings.undeposited_account
    draw = post_entry(
        date=d(6, 30), user=admin_user, memo="Draw", source_type="migration.journal", source_id="J2",
        lines=[LineSpec(expense, debit=D("1000.00")), LineSpec(equity, debit=D("1000.00")),
               LineSpec(und, debit=D("590.00")), LineSpec(bank, credit=D("2590.00"))])
    vendor = post_entry(
        date=d(7, 2), user=admin_user, memo="Payment VP-279", source_type="migration.vendor_payment", source_id="V1",
        lines=[LineSpec(expense, debit=D("1466.00")), LineSpec(und, credit=D("1466.00"))])
    return pays, draw, vendor


def test_payments_plus_named_debits_clear_to_the_cent(draw_case, admin_user):
    pays, draw, vendor = draw_case
    with pytest.raises(DepositError, match="add up to 876.00 but the entry takes out 1,466.00"):
        clear_with_entry(payments=pays, entry=vendor, user=admin_user)
    before = JournalEntry.objects.count()
    clearing = clear_with_entry(payments=pays, entry=vendor, user=admin_user, other_entries=[draw])
    assert JournalEntry.objects.count() == before
    assert list(clearing.other_entries.all()) == [draw]
    assert list(undeposited_payments()) == []
    assert undeposited_check() == [] and close_check(2026, 7) == []


def test_off_by_a_cent_or_wrong_entries_are_refused(draw_case, admin_user, bank, sales_settings):
    pays, draw, vendor = draw_case
    with pytest.raises(DepositError, match="add up to 1,216.00 but"):
        clear_with_entry(payments=pays[:1], entry=vendor, user=admin_user, other_entries=[draw])
    not_a_debit = post_entry(
        date=d(6, 1), user=admin_user, memo="x", source_type="migration.journal", source_id="J3",
        lines=[LineSpec(bank, debit=D("5.00")), LineSpec(sales_settings.undeposited_account, credit=D("5.00"))])
    with pytest.raises(DepositError, match="does not put money into Undeposited Funds"):
        clear_with_entry(payments=pays, entry=vendor, user=admin_user, other_entries=[not_a_debit])
    with pytest.raises(DepositError, match="dated after"):
        clear_with_entry(payments=pays, entry=draw, user=admin_user, other_entries=[vendor])
    assert len(undeposited_payments()) == 2 and not UndepositedClearing.objects.exists()


def test_an_offsetting_entry_is_used_once(draw_case, admin_user, customer):
    pays, draw, vendor = draw_case
    extra = receive_payment(customer=customer, date=d(6, 25), method=PaymentMethod.CASH, amount=D("50.00"), user=admin_user)
    clear_with_entry(payments=pays, entry=vendor, user=admin_user, other_entries=[draw])
    other_vendor = post_entry(
        date=d(7, 3), user=admin_user, memo="more", source_type="migration.vendor_payment", source_id="V2",
        lines=[LineSpec(draw.lines.first().account, debit=D("640.00")),
               LineSpec(vendor.lines.get(credit__gt=0).account, credit=D("640.00"))])
    with pytest.raises(DepositError, match="already accounts for"):
        clear_with_entry(payments=[extra], entry=other_vendor, user=admin_user, other_entries=[draw])


@pytest.mark.parametrize("which", ["draw", "vendor"])
def test_voiding_either_entry_puts_the_payments_back(draw_case, admin_user, which):
    pays, draw, vendor = draw_case
    clear_with_entry(payments=pays, entry=vendor, user=admin_user, other_entries=[draw])
    void_entry({"draw": draw, "vendor": vendor}[which], user=admin_user, reason="Mistake", date=d(8, 1))
    assert len(undeposited_payments()) == 2
    assert undeposited_check(d(7, 15)) == []  # before the void it still counted as cleared

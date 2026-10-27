from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from ledger.balances import account_totals
from sales.errors import DepositError, ReceiptError
from sales.models import DocStatus, PaymentMethod
from sales.posting.deposits import make_deposit, undeposited_payments, void_deposit
from sales.posting.receipts import receive_payment, void_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


def net(account):
    debit, credit = account_totals(start=None, end=d(12, 31)).get(account.pk, (D("0"), D("0")))
    return debit - credit


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def checks(customer, admin_user, sales_settings):
    def pay(amount, day, method=PaymentMethod.CHECK, **kw):
        return receive_payment(customer=customer, date=d(10, day), method=method, amount=D(amount), user=admin_user, **kw)

    return pay("100.00", 3), pay("40.50", 4, PaymentMethod.CASH), pay("10.00", 5)


def test_undeposited_lists_checks_and_cash_oldest_first(checks, customer, admin_user, bank):
    receive_payment(customer=customer, date=d(10, 2), method=PaymentMethod.ACH, amount=D("5.00"), user=admin_user, bank_account=bank)
    assert list(undeposited_payments()) == [checks[0], checks[1], checks[2]]


def test_deposit_groups_payments_and_posts_total(checks, bank, admin_user, sales_settings):
    dep = make_deposit(payments=checks[:2], bank_account=bank, date=d(10, 6), user=admin_user)
    assert dep.number == "DEP-00001" and dep.status == DocStatus.OPEN
    assert entry_lines(dep.journal_entry) == [("1000", D("140.50"), D("0")), ("1250", D("0"), D("140.50"))]
    assert (dep.journal_entry.source_type, dep.journal_entry.source_id) == ("sales.deposit", "DEP-00001")
    assert {p.pk for p in dep.payments.all()} == {checks[0].pk, checks[1].pk}
    assert list(undeposited_payments()) == [checks[2]]
    assert net(sales_settings.undeposited_account) == D("10.00") and net(bank) == D("140.50")


def test_void_then_redeposit(checks, bank, admin_user, sales_settings):
    dep = make_deposit(payments=checks, bank_account=bank, date=d(10, 6), user=admin_user)
    assert list(undeposited_payments()) == []
    out = void_deposit(dep, user=admin_user, reason="Wrong bank", date=d(10, 7))
    assert out.status == DocStatus.VOID and out.voided_on == d(10, 7)
    assert list(undeposited_payments()) == list(checks)
    assert net(sales_settings.undeposited_account) == D("150.50")
    again = make_deposit(payments=checks, bank_account=bank, date=d(10, 8), user=admin_user)
    assert again.number == "DEP-00002" and list(undeposited_payments()) == []


def test_payment_void_blocked_while_deposited(checks, bank, admin_user):
    dep = make_deposit(payments=checks[:1], bank_account=bank, date=d(10, 6), user=admin_user)
    with pytest.raises(ReceiptError, match="PMT-00001 is in deposit DEP-00001. Void the deposit first."):
        void_payment(checks[0], user=admin_user, reason="Bounced")
    void_deposit(dep, user=admin_user, reason="Mistake")
    void_payment(checks[0], user=admin_user, reason="Bounced")
    assert checks[0] not in list(undeposited_payments())


def test_messages(checks, customer, admin_user, bank, ar):
    with pytest.raises(DepositError, match="Choose at least one payment."):
        make_deposit(payments=[], bank_account=bank, date=d(10, 6), user=admin_user)
    dep = make_deposit(payments=checks[:1], bank_account=bank, date=d(10, 6), user=admin_user)
    with pytest.raises(DepositError, match="PMT-00001 is already in deposit DEP-00001."):
        make_deposit(payments=checks[:1], bank_account=bank, date=d(10, 7), user=admin_user)
    ach = receive_payment(customer=customer, date=d(10, 2), method=PaymentMethod.ACH, amount=D("5.00"), user=admin_user, bank_account=bank)
    with pytest.raises(DepositError, match="PMT-00004 was paid by ACH and went straight to the bank."):
        make_deposit(payments=[ach], bank_account=bank, date=d(10, 7), user=admin_user)
    with pytest.raises(DepositError, match="The deposit date can't be before PMT-00003's date."):
        make_deposit(payments=checks[2:], bank_account=bank, date=d(10, 4), user=admin_user)
    with pytest.raises(DepositError, match="Choose the bank account for the deposit."):
        make_deposit(payments=checks[2:], bank_account=ar, date=d(10, 7), user=admin_user)


def test_failed_deposit_saves_nothing(checks, bank, admin_user):
    with pytest.raises(DepositError):
        make_deposit(payments=[checks[0], checks[2]], bank_account=bank, date=d(10, 4), user=admin_user)
    assert len(list(undeposited_payments())) == 3


def test_standard_user_can_deposit_but_not_void(checks, bank, standard_user):
    dep = make_deposit(payments=checks[:1], bank_account=bank, date=d(10, 6), user=standard_user)
    with pytest.raises(PermissionDenied):
        void_deposit(dep, user=standard_user, reason="x")


def test_void_deposit_needs_reason_and_is_final(checks, bank, admin_user):
    dep = make_deposit(payments=checks[:1], bank_account=bank, date=d(10, 6), user=admin_user)
    with pytest.raises(DepositError, match="Enter a reason."):
        void_deposit(dep, user=admin_user, reason="")
    void_deposit(dep, user=admin_user, reason="x")
    with pytest.raises(DepositError, match="DEP-00001 is already void."):
        void_deposit(dep, user=admin_user, reason="y")


def test_redeposit_cannot_predate_the_void(checks, bank, admin_user):
    # Otherwise the payment would sit in two deposits between the two dates (Phase 3 review finding).
    from sales.checks import undeposited_check

    dep = make_deposit(payments=[checks[0]], bank_account=bank, date=d(10, 6), user=admin_user)
    void_deposit(dep, user=admin_user, reason="Wrong bank", date=d(10, 20))
    with pytest.raises(DepositError, match="The deposit date can't be before 2026-10-20, when PMT-00001 was taken out of DEP-00001."):
        make_deposit(payments=[checks[0]], bank_account=bank, date=d(10, 10), user=admin_user)
    make_deposit(payments=[checks[0]], bank_account=bank, date=d(10, 20), user=admin_user)
    for day in (6, 10, 19, 20, 21):
        assert undeposited_check(d(10, day)) == []

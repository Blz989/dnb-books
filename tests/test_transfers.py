from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from banking import inbox
from banking.models import (
    AccountKind, BankAccount, BankTransaction, Match, TransferKind, TransferStatus, TxnSource, TxnStatus,
)
from banking.transfers import TransferError, create_transfer, void_transfer
from ledger.balances import account_totals
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def savings(db):
    acct = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    return BankAccount.objects.create(ledger_account=acct, kind=AccountKind.SAVINGS)


def lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


def balance(account):
    debit, credit = account_totals(start=None, end=date.max).get(account.pk, (D("0.00"), D("0.00")))
    return debit - credit


def row(account, amount, on, ident):
    return BankTransaction.objects.create(
        bank_account=account, external_id=ident, date=on, description="x", amount=D(amount), source=TxnSource.FILE)


def test_plain_transfer_entry(admin_user, checking, savings):
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("500.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)
    assert lines(t.journal_entry) == [("1000", D("0.00"), D("500.00")), ("1010", D("500.00"), D("0.00"))]
    assert (t.journal_entry.source_type, t.journal_entry.source_id) == ("banking.transfer", f"TR-{t.pk}")
    assert t.status == TransferStatus.POSTED


def test_card_payoff_entry(admin_user, checking, card_account, card):
    t = create_transfer(date=d(1, 20), from_account=checking, to_account=card_account, amount=D("500.00"),
                        kind=TransferKind.CARD_PAYOFF, user=admin_user)
    assert lines(t.journal_entry) == [("1000", D("0.00"), D("500.00")), ("2100", D("500.00"), D("0.00"))]


def test_loan_payment_entry_splits_principal_and_interest(admin_user, checking, loan, interest_expense):
    t = create_transfer(date=d(1, 15), from_account=checking, to_account=loan, amount=D("400.00"),
                        kind=TransferKind.LOAN_PAYMENT, principal=D("340.00"), interest=D("60.00"),
                        interest_account=interest_expense, user=admin_user)
    assert lines(t.journal_entry) == [
        ("1000", D("0.00"), D("400.00")), ("2500", D("340.00"), D("0.00")), ("6800", D("60.00"), D("0.00"))]
    assert (t.principal, t.interest) == (D("340.00"), D("60.00"))


def test_loan_payment_with_no_interest_skips_the_zero_line(admin_user, checking, loan, interest_expense):
    t = create_transfer(date=d(1, 15), from_account=checking, to_account=loan, amount=D("400.00"),
                        kind=TransferKind.LOAN_PAYMENT, principal=D("400.00"), interest=D("0.00"),
                        interest_account=interest_expense, user=admin_user)
    assert lines(t.journal_entry) == [("1000", D("0.00"), D("400.00")), ("2500", D("400.00"), D("0.00"))]


def test_loan_sum_message(admin_user, checking, loan, interest_expense):
    with pytest.raises(TransferError, match=r"Principal and interest must add up to 400\.00\."):
        create_transfer(date=d(1, 15), from_account=checking, to_account=loan, amount=D("400.00"),
                        kind=TransferKind.LOAN_PAYMENT, principal=D("340.00"), interest=D("59.00"),
                        interest_account=interest_expense, user=admin_user)


def test_loan_interest_needs_an_interest_account(admin_user, checking, loan):
    with pytest.raises(TransferError, match="Choose the interest account."):
        create_transfer(date=d(1, 15), from_account=checking, to_account=loan, amount=D("400.00"),
                        kind=TransferKind.LOAN_PAYMENT, principal=D("340.00"), interest=D("60.00"), user=admin_user)


def test_same_account_message(admin_user, checking):
    with pytest.raises(TransferError, match="Choose two different accounts."):
        create_transfer(date=d(1, 5), from_account=checking, to_account=checking, amount=D("5.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)


def test_card_payoff_needs_card_account(admin_user, checking, savings):
    with pytest.raises(TransferError, match="A card payoff goes to a credit card account."):
        create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("5.00"),
                        kind=TransferKind.CARD_PAYOFF, user=admin_user)


def test_loan_payment_needs_loan_account(admin_user, checking, savings):
    with pytest.raises(TransferError, match="A loan payment goes to a loan account."):
        create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("5.00"),
                        kind=TransferKind.LOAN_PAYMENT, principal=D("5.00"), interest=D("0.00"), user=admin_user)


def test_amount_must_be_positive(admin_user, checking, savings):
    with pytest.raises(TransferError, match="Enter an amount above zero."):
        create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("0.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)


def test_card_payoff_matches_from_both_sides_and_books_once(admin_user, checking, card_account, card, bank):
    out_row = row(checking, "-500.00", d(1, 20), "chk")
    in_row = row(card_account, "500.00", d(1, 21), "card")
    t = create_transfer(date=d(1, 20), from_account=checking, to_account=card_account, amount=D("500.00"),
                        kind=TransferKind.CARD_PAYOFF, user=admin_user, from_txn=out_row)
    out_row.refresh_from_db()
    assert out_row.status == TxnStatus.MATCHED
    assert Match.objects.get(bank_transaction=out_row).journal_entry == t.journal_entry

    cand = inbox.suggest(in_row)
    assert (cand.kind, cand.target, cand.score) == ("transfer", t, 74)
    before = (balance(bank), balance(card))
    inbox.confirm(in_row, cand, user=admin_user)
    in_row.refresh_from_db()
    assert in_row.status == TxnStatus.MATCHED
    assert (balance(bank), balance(card)) == before  # nothing new booked
    assert Match.objects.filter(journal_entry=t.journal_entry).count() == 2


def test_transfer_is_not_offered_again_on_a_side_already_matched(admin_user, checking, savings):
    out_row = row(checking, "-50.00", d(1, 5), "a")
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user, from_txn=out_row)
    another_out = row(checking, "-50.00", d(1, 5), "b")
    assert inbox.candidates(another_out) == []
    in_row = row(savings, "50.00", d(1, 6), "c")
    assert [c.target for c in inbox.candidates(in_row)] == [t]


def test_transfer_candidate_needs_right_direction_amount_and_window(admin_user, checking, savings):
    create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                    kind=TransferKind.TRANSFER, user=admin_user)
    assert inbox.candidates(row(savings, "-50.00", d(1, 5), "wrong-sign")) == []
    assert inbox.candidates(row(savings, "51.00", d(1, 5), "wrong-amount")) == []
    assert inbox.candidates(row(savings, "50.00", d(1, 11), "too-late")) == []
    assert len(inbox.candidates(row(savings, "50.00", d(1, 10), "edge"))) == 1


def test_confirming_the_same_side_twice_is_refused(admin_user, checking, savings):
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)
    first, second = row(savings, "50.00", d(1, 5), "a"), row(savings, "50.00", d(1, 5), "b")
    stale = inbox.suggest(second)
    inbox.confirm(first, inbox.suggest(first), user=admin_user)
    with pytest.raises(inbox.InboxError, match="That item is already matched to another bank transaction."):
        inbox.confirm(second, stale, user=admin_user)
    assert t.pk


def test_void_transfer_reverses_and_returns_bank_rows(admin_user, checking, savings, bank):
    out_row = row(checking, "-50.00", d(1, 5), "a")
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user, from_txn=out_row)
    void_transfer(t, user=admin_user, reason="Wrong amount")
    t.refresh_from_db()
    out_row.refresh_from_db()
    assert t.status == TransferStatus.VOID and t.void_reason == "Wrong amount" and t.voided_on == d(1, 5)
    assert t.journal_entry.status == EntryStatus.VOID
    assert out_row.status == TxnStatus.NEW
    assert balance(bank) == D("0.00")
    assert inbox.candidates(row(savings, "50.00", d(1, 5), "late")) == []  # void transfers aren't offered


def test_void_transfer_is_admin_only(standard_user, admin_user, checking, savings):
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)
    with pytest.raises(PermissionDenied):
        void_transfer(t, user=standard_user, reason="x")


def test_void_transfer_needs_a_reason(admin_user, checking, savings):
    t = create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user)
    with pytest.raises(TransferError, match="Enter a reason."):
        void_transfer(t, user=admin_user, reason=" ")


def test_from_txn_must_be_on_one_of_the_accounts(admin_user, checking, savings, card_account):
    stray = row(card_account, "-50.00", d(1, 5), "a")
    with pytest.raises(TransferError, match="That bank transaction isn't on either account."):
        create_transfer(date=d(1, 5), from_account=checking, to_account=savings, amount=D("50.00"),
                        kind=TransferKind.TRANSFER, user=admin_user, from_txn=stray)
    assert not BankTransaction.objects.filter(status=TxnStatus.MATCHED).exists()

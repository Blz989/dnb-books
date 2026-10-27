from datetime import date
from decimal import Decimal

import pytest

from banking import inbox
from banking.inbox import InboxError
from banking.models import (
    BankTransaction, Match, PatternType, Rule, TxnSource, TxnStatus,
)
from ledger.balances import account_totals
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.payments import pay_vendor, void_vendor_payment
from sales.models import PaymentMethod
from sales.posting.deposits import make_deposit
from sales.posting.receipts import receive_payment, void_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def fee_expense(db):
    return Account.objects.create(number="6900", name="Bank fees", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)


@pytest.fixture
def make_txn(checking):
    counter = iter(range(1, 1000))

    def factory(amount, on=d(1, 10), description="Some bank text", account=None, check_number=""):
        return BankTransaction.objects.create(
            bank_account=account or checking, external_id=f"x{next(counter)}", date=on, description=description,
            amount=D(amount), check_number=check_number, source=TxnSource.FILE)

    return factory


def totals(account):
    debit, credit = account_totals(start=None, end=date.max).get(account.pk, (D("0.00"), D("0.00")))
    return debit - credit


def test_vendor_check_with_number_scores_100_and_beats_amount_only(setup, admin_user, bank, vendor, make_txn, ap):
    from contacts.models import Vendor

    other = Vendor.objects.create(name="Other Supply")
    check = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("420.00"), paid_from=bank,
                       user=admin_user, check_number="2050")
    ach = pay_vendor(vendor=other, date=d(1, 10), method=VM.ACH, amount=D("420.00"), paid_from=bank, user=admin_user)
    txn = make_txn("-420.00", on=d(1, 10), check_number="2050")
    found = inbox.candidates(txn)
    assert [(c.kind, c.target, c.score) for c in found] == [("vendor_check", check, 100), ("document", ach, 80)]
    assert inbox.suggest(txn).target == check


def test_check_number_compares_without_leading_zeros(setup, admin_user, bank, vendor, make_txn):
    check = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("420.00"), paid_from=bank,
                       user=admin_user, check_number="2050")
    txn = make_txn("-420.00", check_number="002050")
    assert inbox.suggest(txn).kind == "vendor_check" and inbox.suggest(txn).target == check


def test_check_number_with_wrong_amount_is_not_a_candidate(setup, admin_user, bank, vendor, make_txn):
    pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("420.00"), paid_from=bank,
               user=admin_user, check_number="2050")
    assert inbox.candidates(make_txn("-421.00", check_number="2050")) == []


def test_deposit_total_matches_within_five_days(setup, admin_user, bank, customer, make_txn):
    p1 = receive_payment(customer=customer, date=d(1, 2), method=PaymentMethod.CHECK, amount=D("300.00"), user=admin_user)
    p2 = receive_payment(customer=customer, date=d(1, 2), method=PaymentMethod.CASH, amount=D("200.00"), user=admin_user)
    dep = make_deposit(payments=[p1, p2], bank_account=bank, date=d(1, 3), user=admin_user)
    near = inbox.candidates(make_txn("500.00", on=d(1, 5)))
    assert [(c.kind, c.target, c.score) for c in near] == [("deposit", dep, 83)]
    assert inbox.candidates(make_txn("500.00", on=d(1, 9))) == []  # 6 days
    assert inbox.candidates(make_txn("300.00", on=d(1, 3))) == []  # one payment is not the deposit


def test_ach_customer_payment_within_ten_days_not_eleven(setup, admin_user, bank, customer, make_txn):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                          user=admin_user, bank_account=bank)
    ten = inbox.candidates(make_txn("75.00", on=d(1, 11)))
    assert [(c.kind, c.target, c.score) for c in ten] == [("document", pay, 70)]
    assert inbox.candidates(make_txn("75.00", on=d(1, 12))) == []


def test_money_in_never_matches_money_out_documents(setup, admin_user, bank, vendor, make_txn):
    pay_vendor(vendor=vendor, date=d(1, 5), method=VM.ACH, amount=D("50.00"), paid_from=bank, user=admin_user)
    assert inbox.candidates(make_txn("50.00", on=d(1, 5))) == []


def test_card_purchase_matches_company_card_payment(setup, admin_user, card, card_account, vendor, make_txn):
    pay = pay_vendor(vendor=vendor, date=d(1, 4), method=VM.CARD, amount=D("86.40"), paid_from=card, user=admin_user)
    txn = make_txn("-86.40", on=d(1, 4), account=card_account)
    assert inbox.suggest(txn).target == pay


def test_documents_for_other_accounts_and_void_ones_are_not_offered(
        setup, admin_user, bank, vendor, make_txn, card, card_account):
    on_card = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CARD, amount=D("20.00"), paid_from=card, user=admin_user)
    voided = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.ACH, amount=D("20.00"), paid_from=bank, user=admin_user)
    void_vendor_payment(voided, user=admin_user, reason="oops")
    assert inbox.candidates(make_txn("-20.00", on=d(1, 5))) == []
    assert on_card.pk  # exists, just on the other account


def test_ties_break_by_closer_date_then_lower_number(setup, admin_user, bank, vendor, make_txn):
    a = pay_vendor(vendor=vendor, date=d(1, 8), method=VM.ACH, amount=D("10.00"), paid_from=bank, user=admin_user)
    b = pay_vendor(vendor=vendor, date=d(1, 12), method=VM.ACH, amount=D("10.00"), paid_from=bank, user=admin_user)
    c = pay_vendor(vendor=vendor, date=d(1, 8), method=VM.ACH, amount=D("10.00"), paid_from=bank, user=admin_user)
    found = inbox.candidates(make_txn("-10.00", on=d(1, 10)))
    # a and b are 2 days away, c too; all score 78. Equal distance falls back to the lower number.
    assert [x.target for x in found] == [a, b, c]
    assert {x.score for x in found} == {78}


def test_confirm_document_links_only(setup, admin_user, bank, customer, make_txn):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                          user=admin_user, bank_account=bank)
    before = totals(bank)
    txn = make_txn("75.00", on=d(1, 2))
    match = inbox.confirm(txn, inbox.suggest(txn), user=admin_user)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    assert match.sales_payment == pay and match.confirmed_by == admin_user
    assert totals(bank) == before


def test_matched_target_is_not_offered_again_and_cannot_be_confirmed(setup, admin_user, bank, customer, make_txn):
    receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                    user=admin_user, bank_account=bank)
    first, second = make_txn("75.00", on=d(1, 2)), make_txn("75.00", on=d(1, 3))
    stale = inbox.suggest(second)
    inbox.confirm(first, inbox.suggest(first), user=admin_user)
    assert inbox.candidates(second) == []
    with pytest.raises(InboxError, match="That item is already matched to another bank transaction."):
        inbox.confirm(second, stale, user=admin_user)
    second.refresh_from_db()
    assert second.status == TxnStatus.NEW


def test_confirm_requires_a_new_transaction(setup, admin_user, bank, customer, make_txn):
    receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                    user=admin_user, bank_account=bank)
    txn = make_txn("75.00", on=d(1, 2))
    cand = inbox.suggest(txn)
    inbox.confirm(txn, cand, user=admin_user)
    with pytest.raises(InboxError, match="This bank transaction was already handled."):
        inbox.confirm(txn, cand, user=admin_user)


def test_rule_suggests_and_confirm_books_entry_and_moves_trial_balance(setup, admin_user, bank, fee_expense, make_txn):
    Rule.objects.create(pattern="service fee", pattern_type=PatternType.CONTAINS, account=fee_expense, memo="Bank fee")
    txn = make_txn("-15.00", description="MONTHLY SERVICE FEE")
    cand = inbox.suggest(txn)
    assert (cand.kind, cand.score) == ("rule", 50)
    match = inbox.confirm(txn, cand, user=admin_user)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.BOOKED
    entry = match.journal_entry
    assert (entry.source_type, entry.source_id, entry.date, entry.memo) == (
        "banking.entry", f"BT-{txn.pk}", d(1, 10), "Bank fee")
    assert entry.status == EntryStatus.POSTED
    assert sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account")) == [
        ("1000", D("0.00"), D("15.00")), ("6900", D("15.00"), D("0.00"))]
    assert totals(bank) == D("-15.00") and totals(fee_expense) == D("15.00")


def test_new_entry_for_money_in_debits_the_bank(setup, admin_user, bank, revenue, make_txn):
    txn = make_txn("40.00", description="Interest")
    inbox.new_entry(txn, account=revenue, user=admin_user)
    assert totals(bank) == D("40.00") and totals(revenue) == D("-40.00")


def test_new_entry_on_card_purchase_credits_the_card(setup, admin_user, card, card_account, expense, make_txn):
    txn = make_txn("-86.40", description="OFFICE DEPOT", account=card_account)
    inbox.new_entry(txn, account=expense, user=admin_user)
    assert totals(card) == D("-86.40") and totals(expense) == D("86.40")


def test_rules_use_position_scope_and_active_flag(setup, admin_user, checking, card_account, fee_expense, expense, make_txn):
    Rule.objects.create(position=3, pattern="fee", account=expense)
    off = Rule.objects.create(position=0, pattern="fee", account=expense, is_active=False)
    elsewhere = Rule.objects.create(position=1, pattern="fee", account=expense, bank_account=card_account)
    starts = Rule.objects.create(position=2, pattern="MONTHLY", pattern_type=PatternType.STARTS_WITH, account=fee_expense)
    txn = make_txn("-15.00", description="monthly service FEE")
    assert inbox.suggest(txn).target == starts
    assert {off, elsewhere}.isdisjoint({c.target for c in inbox.candidates(txn)})
    assert inbox.suggest(make_txn("-1.00", description="late fee")).target.position == 3  # CONTAINS rule, STARTS_WITH misses


def test_bulk_confirm_skips_rows_without_a_suggestion(setup, admin_user, bank, customer, fee_expense, make_txn):
    receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                    user=admin_user, bank_account=bank)
    Rule.objects.create(pattern="fee", account=fee_expense)
    rows = [make_txn("75.00", on=d(1, 2)), make_txn("-15.00", description="fee"), make_txn("-3.33", description="???")]
    assert inbox.bulk_confirm(rows, user=admin_user) == 2
    assert [BankTransaction.objects.get(pk=r.pk).status for r in rows] == [
        TxnStatus.MATCHED, TxnStatus.BOOKED, TxnStatus.NEW]


def test_bulk_confirm_skips_a_row_whose_suggestion_was_just_taken(setup, admin_user, bank, customer, make_txn):
    receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                    user=admin_user, bank_account=bank)
    rows = [make_txn("75.00", on=d(1, 2)), make_txn("75.00", on=d(1, 3))]
    assert inbox.bulk_confirm(rows, user=admin_user) == 1


def test_exclude_needs_reason_then_restore(setup, admin_user, make_txn):
    txn = make_txn("-5.00")
    with pytest.raises(InboxError, match="Enter a reason."):
        inbox.exclude(txn, reason="  ", user=admin_user)
    inbox.exclude(txn, reason="Personal", user=admin_user)
    txn.refresh_from_db()
    assert (txn.status, txn.exclude_reason) == (TxnStatus.EXCLUDED, "Personal")
    inbox.restore(txn, user=admin_user)
    txn.refresh_from_db()
    assert (txn.status, txn.exclude_reason) == (TxnStatus.NEW, "")


def test_voiding_a_matched_payment_returns_the_transaction_to_new(setup, admin_user, bank, customer, make_txn):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("75.00"),
                          user=admin_user, bank_account=bank)
    txn = make_txn("75.00", on=d(1, 2))
    inbox.confirm(txn, inbox.suggest(txn), user=admin_user)
    void_payment(pay, user=admin_user, reason="Wrong customer")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW
    assert not Match.objects.filter(bank_transaction=txn).exists()
    assert inbox.candidates(txn) == []  # the void payment is not offered


def test_voiding_a_matched_vendor_check_returns_the_transaction_to_new(setup, admin_user, bank, vendor, make_txn):
    pay = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("420.00"), paid_from=bank,
                     user=admin_user, check_number="2050")
    txn = make_txn("-420.00", check_number="2050")
    inbox.confirm(txn, inbox.suggest(txn), user=admin_user)
    void_vendor_payment(pay, user=admin_user, reason="Lost")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW


def test_voiding_an_inbox_entry_returns_the_transaction_to_new(setup, admin_user, fee_expense, make_txn):
    from ledger.posting import void_entry

    txn = make_txn("-15.00")
    match = inbox.new_entry(txn, account=fee_expense, user=admin_user)
    void_entry(match.journal_entry, user=admin_user, reason="Wrong account")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW and not Match.objects.filter(bank_transaction=txn).exists()

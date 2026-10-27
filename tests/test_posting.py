import threading
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.models import Sum
from hypothesis import given, settings as hyp_settings, strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase

from ledger.errors import (
    EntryStateError, InactiveAccountError, InvalidLineError, PeriodClosedError, UnbalancedEntryError,
)
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus, JournalEntry, JournalLine
from ledger.periods import close_period
from ledger.posting import LineSpec, post_draft, post_entry

D = Decimal


def test_post_balanced_entry(admin_user, cash, revenue):
    e = post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
        LineSpec(cash, debit=D("100.00")), LineSpec(revenue, credit=D("100.00"))])
    assert e.status == EntryStatus.POSTED and e.lines.count() == 2
    assert e.posted_by == admin_user and e.posted_at is not None and e.created_by == admin_user


def test_unbalanced_rejected_and_nothing_saved(admin_user, cash, revenue):
    with pytest.raises(UnbalancedEntryError, match="Difference: 0.01"):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("100.00")), LineSpec(revenue, credit=D("99.99"))])
    assert JournalEntry.objects.count() == 0


def test_unbalanced_message_format(admin_user, cash, revenue):
    with pytest.raises(UnbalancedEntryError) as exc:
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("1500")), LineSpec(revenue, credit=D("1000"))])
    assert str(exc.value) == "Debits (1,500.00) and credits (1,000.00) must be equal. Difference: 500.00."


def test_needs_two_lines(admin_user, cash):
    with pytest.raises(InvalidLineError, match="An entry needs at least two lines."):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[LineSpec(cash, debit=D("1.00"))])


@pytest.mark.parametrize("amount,msg", [
    (D("-5.00"), "Line 1: amounts can't be negative"),
    (D("1.005"), "Line 1: amounts can have at most 2 decimal places")])
def test_bad_amounts(admin_user, cash, revenue, amount, msg):
    with pytest.raises(InvalidLineError, match=msg):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=amount), LineSpec(revenue, credit=D("5.00"))])
    assert JournalEntry.objects.count() == 0


def test_float_amounts_refused(admin_user, cash, revenue):
    with pytest.raises(TypeError):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=1.5), LineSpec(revenue, credit=D("1.50"))])


def test_both_debit_and_credit_rejected(admin_user, cash, revenue):
    with pytest.raises(InvalidLineError, match=r"Line 2: enter a debit or a credit, not both\."):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("5.00")), LineSpec(revenue, debit=D("1.00"), credit=D("5.00"))])


def test_empty_line_rejected(admin_user, cash, revenue):
    with pytest.raises(InvalidLineError, match=r"Line 2: enter an amount\."):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("5.00")), LineSpec(revenue)])


def test_inactive_account_rejected(admin_user, cash, revenue):
    revenue.is_active = False
    revenue.save()
    with pytest.raises(InactiveAccountError, match=r"Line 2: account 4000 Sales is inactive\."):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])


def test_closed_period_rejected(admin_user, cash, revenue):
    close_period(2026, 1, admin_user)
    with pytest.raises(PeriodClosedError, match="January 2026 is closed"):
        post_entry(date=date(2026, 1, 31), user=admin_user, lines=[
            LineSpec(cash, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    assert JournalEntry.objects.count() == 0


def test_post_draft_validates_existing_lines(draft_entry, admin_user, cash):
    JournalLine.objects.create(entry=draft_entry, account=cash, debit=D("7.00"))
    with pytest.raises(UnbalancedEntryError):
        post_draft(draft_entry, admin_user)
    draft_entry.refresh_from_db()
    assert draft_entry.status == EntryStatus.DRAFT


def test_post_draft_posts_balanced_draft(draft_entry, admin_user):
    e = post_draft(draft_entry, admin_user)
    assert e.status == EntryStatus.POSTED and e.posted_by == admin_user and e.posted_at is not None


def test_post_draft_rejects_non_draft(posted_entry, admin_user):
    with pytest.raises(EntryStateError, match="Only draft entries can be posted."):
        post_draft(posted_entry, admin_user)


class LedgerBalanceProperty(HypothesisTestCase):
    """Each hypothesis example runs in a fresh transaction."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model

        cls.user = get_user_model().objects.create_user("prop", password="pw")
        cls.cash = Account.objects.create(number="1000", name="Cash", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
        cls.rev = Account.objects.create(number="4000", name="Sales", type=AccountType.INCOME, subtype=AccountSubtype.INCOME)

    @hyp_settings(max_examples=40, deadline=None)
    @given(st.lists(st.decimals(min_value=D("0.01"), max_value=D("99999.99"), places=2), min_size=1, max_size=10))
    def test_ledger_always_balances(self, amounts):
        for a in amounts:
            post_entry(date=date(2026, 1, 5), user=self.user, lines=[
                LineSpec(self.cash, debit=a), LineSpec(self.rev, credit=a)])
        totals = JournalLine.objects.in_ledger().aggregate(d=Sum("debit"), c=Sum("credit"))
        assert totals["d"] == totals["c"] == sum(amounts)


@pytest.mark.django_db(transaction=True)
def test_concurrent_posts_both_succeed(admin_user, cash, revenue, ledger_purge):
    errors = []

    def worker():
        try:
            for _ in range(20):
                post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
                    LineSpec(cash, debit=D("1.00")), LineSpec(revenue, credit=D("1.00"))])
        except Exception as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert JournalEntry.objects.filter(status=EntryStatus.POSTED).count() == 40

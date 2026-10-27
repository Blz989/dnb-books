import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

from core.roles import ADMIN_GROUP, STANDARD_GROUP


def _user(username, group_name):
    call_command("setup_roles")
    user = get_user_model().objects.create_user(username, password="pw", is_staff=True)
    user.groups.add(Group.objects.get(name=group_name))
    return user


@pytest.fixture
def standard_user(db):
    return _user("standard", STANDARD_GROUP)


@pytest.fixture
def admin_user(db):
    return _user("admin", ADMIN_GROUP)


# ---- ledger fixtures (Task 3) ----
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from ledger.models import (  # noqa: E402
    Account, AccountSubtype, AccountType, EntryStatus, JournalEntry, JournalLine,
)


def _account(number, name, type_, subtype):
    return Account.objects.create(number=number, name=name, type=type_, subtype=subtype)


@pytest.fixture
def cash(db):
    return _account("1000", "Cash", AccountType.ASSET, AccountSubtype.BANK)


@pytest.fixture
def ar(db):
    return _account("1200", "Accounts Receivable", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE)


@pytest.fixture
def ap(db):
    return _account("2000", "Accounts Payable", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE)


@pytest.fixture
def equity(db):
    return _account("3000", "Owner's Equity", AccountType.EQUITY, AccountSubtype.EQUITY)


@pytest.fixture
def revenue(db):
    return _account("4000", "Sales", AccountType.INCOME, AccountSubtype.INCOME)


@pytest.fixture
def expense(db):
    return _account("6000", "Office Expense", AccountType.EXPENSE, AccountSubtype.EXPENSE)


@pytest.fixture
def draft_entry(db, admin_user, cash, revenue):
    entry = JournalEntry.objects.create(date=date(2026, 1, 15), memo="Draft", created_by=admin_user)
    JournalLine.objects.create(entry=entry, account=cash, debit=Decimal("100.00"))
    JournalLine.objects.create(entry=entry, account=revenue, credit=Decimal("100.00"))
    return entry


@pytest.fixture
def posted_entry(draft_entry):
    # Task 5 switches this to post_entry.
    JournalEntry.objects.filter(pk=draft_entry.pk).update(status=EntryStatus.POSTED)
    draft_entry.refresh_from_db()
    return draft_entry

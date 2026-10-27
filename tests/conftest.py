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
def posted_entry(admin_user, cash, revenue):
    from ledger.posting import LineSpec, post_entry

    return post_entry(
        date=date(2026, 1, 15), user=admin_user, memo="Posted",
        lines=[LineSpec(cash, debit=Decimal("100.00")), LineSpec(revenue, credit=Decimal("100.00"))],
    )


@pytest.fixture
def ledger_purge():
    """For transactional tests that leave posted entries behind.

    Django's end-of-test flush DELETEs every row, which the immutability
    triggers correctly refuse. This empties the ledger tables with the triggers
    off, then reinstalls the exact triggers from migration 0002. Request it as
    the last fixture so it tears down before the database flush.
    """
    import importlib

    from django.db import connection

    yield
    mig = importlib.import_module("ledger.migrations.0002_immutability_triggers")
    with connection.cursor() as c:
        for sql in mig.REVERSE:
            c.execute(sql)
        c.execute("DELETE FROM ledger_journalline")
        c.execute("DELETE FROM ledger_journalentry")
        for sql in mig.FORWARD:
            c.execute(sql)


@pytest.fixture
def sample_ledger(admin_user, cash, ar, equity, revenue, expense):
    """Owner invests, two sales across a year end, one expense.

    Also holds one draft and one voided entry; neither may change any report.
    """
    from ledger.posting import LineSpec, post_entry, void_entry

    D = Decimal

    def post(d, debit_acct, credit_acct, amount):
        return post_entry(date=d, user=admin_user, lines=[
            LineSpec(debit_acct, debit=D(amount)), LineSpec(credit_acct, credit=D(amount))])

    post(date(2025, 6, 1), cash, equity, "10000.00")
    post(date(2025, 12, 31), cash, revenue, "1000.00")
    post(date(2026, 1, 1), expense, cash, "300.00")
    post(date(2026, 1, 31), ar, revenue, "500.00")

    voided = post(date(2026, 1, 15), expense, cash, "50.00")
    void_entry(voided, user=admin_user, reason="Entered twice")

    draft = JournalEntry.objects.create(date=date(2026, 1, 20), memo="Draft", created_by=admin_user)
    JournalLine.objects.create(entry=draft, account=cash, debit=D("9999.00"))
    JournalLine.objects.create(entry=draft, account=revenue, credit=D("9999.00"))

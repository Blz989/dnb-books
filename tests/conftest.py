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


@pytest.fixture
def file_db_with_entries(transactional_db, ledger_purge, settings, admin_user, cash, revenue):
    """The file-backed test database with a few committed, posted entries and a backup key."""
    from pathlib import Path

    from cryptography.fernet import Fernet
    from django.db import connection

    from ledger.posting import LineSpec, post_entry

    settings.DNB_DB_PATH = Path(connection.settings_dict["NAME"])
    settings.DNB_BACKUP_KEY = Fernet.generate_key().decode()
    for amount in ("100.00", "250.50", "19.99"):
        post_entry(date=date(2026, 1, 5), user=admin_user, lines=[
            LineSpec(cash, debit=Decimal(amount)), LineSpec(revenue, credit=Decimal(amount))])


# ---- items fixtures (Phase 2, Task 2) ----
@pytest.fixture
def inventory_account(db):
    return _account("1300", "Inventory Asset", AccountType.ASSET, AccountSubtype.INVENTORY)


@pytest.fixture
def cogs_account(db):
    return _account("5000", "Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)


@pytest.fixture
def shrinkage_account(db):
    return _account("6500", "Inventory Shrinkage", AccountType.EXPENSE, AccountSubtype.EXPENSE)


@pytest.fixture
def writedown_account(db):
    return _account("6510", "Inventory Write-down", AccountType.EXPENSE, AccountSubtype.EXPENSE)


@pytest.fixture
def opening_equity(db):
    return _account("3900", "Opening Balance Equity", AccountType.EQUITY, AccountSubtype.EQUITY)


@pytest.fixture
def inv_settings(db, inventory_account, cogs_account, revenue, shrinkage_account, writedown_account, opening_equity):
    from items.models import InventorySettings

    s = InventorySettings.load()
    s.default_inventory_account = inventory_account
    s.default_cogs_account = cogs_account
    s.default_income_account = revenue
    s.shrinkage_account = shrinkage_account
    s.writedown_account = writedown_account
    s.opening_equity_account = opening_equity
    s.save()
    return s


@pytest.fixture
def make_item(db, inventory_account, cogs_account, revenue):
    from items.models import CostingMethod, Item, ItemType

    def factory(sku, method=CostingMethod.FIFO, purchase_cost="1.00", **kwargs):
        fields = dict(
            sku=sku, name=f"Item {sku}", item_type=ItemType.STOCK, unit="each",
            purchase_cost=Decimal(purchase_cost), costing_method=method,
            income_account=revenue, cogs_account=cogs_account, inventory_account=inventory_account,
        )
        fields.update(kwargs)
        return Item.objects.create(**fields)

    return factory


@pytest.fixture
def items_purge():
    """Like ledger_purge, for the items tables. List it before ledger_purge."""
    import importlib

    from django.db import connection

    yield
    mig = importlib.import_module("items.migrations.0002_immutability_triggers")
    with connection.cursor() as c:
        for sql in mig.REVERSE:
            c.execute(sql)
        for table in ("items_costrevision", "items_fifolayer", "items_stockmovement"):
            c.execute(f"DELETE FROM {table}")
        for sql in mig.FORWARD:
            c.execute(sql)


# ---- contacts fixtures (Phase 3, Task 2) ----
@pytest.fixture
def customer(db):
    from contacts.models import Customer, Terms

    return Customer.objects.create(
        name="Acme Builders", terms=Terms.NET_30,
        billing_address="100 Main St\nSpringfield, IL 62701",
        ship_to_address="Dock 4, 200 Industrial Way\nSpringfield, IL 62702",
    )


# ---- sales fixtures (Phase 3, Task 3) ----
@pytest.fixture
def bank(cash):
    return cash


@pytest.fixture
def sales_settings(db, ar):
    from sales.models import SalesSettings

    undeposited = _account("1250", "Undeposited Funds", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET)
    returns = _account("4900", "Sales returns and allowances", AccountType.INCOME, AccountSubtype.INCOME)
    s = SalesSettings.load()
    s.ar_account = ar
    s.undeposited_account = undeposited
    s.returns_account = returns
    s.company_name = "DNB Supply"
    s.save()
    return s

from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction

from banking.models import AccountKind, BankAccount
from contacts.models import Customer, Terms, Vendor
from items.models import InventorySettings, Item, MovementKind, StockMovement
from ledger.models import Account, JournalLine
from migration.models import ZohoRecord
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from purchasing.models import PurchasingSettings
from sales.errors import SalesError
from sales.models import DocType, Payment, PaymentMethod, SalesSettings
from sales.numbering import next_number, use_number
from tests.zoho_sample import BANK, CARD, LOC_CHASE, LOC_HEADWAY, UNDEPOSITED, build_sample_export


@pytest.fixture
def replay(db, admin_user, tmp_path):
    r = Replay(read_export(build_sample_export(tmp_path)), admin_user)
    r.load_master()
    return r


def test_use_number_forces_one_number(db):
    with transaction.atomic():
        with use_number(DocType.PMT, "CP-1002"):
            assert next_number(DocType.PMT) == "CP-1002"
            assert next_number(DocType.PMT) == "PMT-00001"
        assert next_number(DocType.PMT) == "PMT-00002"


def test_use_number_is_cleared_on_exit_unused(db):
    with transaction.atomic():
        with use_number(DocType.PMT, "CP-1002"):
            pass
        assert next_number(DocType.PMT) == "PMT-00001"


def test_use_number_applies_to_its_own_doc_type(db):
    with transaction.atomic():
        with use_number(DocType.PMT, "CP-1002"):
            assert next_number(DocType.INV) == "INV-000001"
            assert next_number(DocType.PMT) == "CP-1002"


def test_use_number_refuses_used(replay):
    customer = Customer.objects.get(name="Acme Hardware")
    Payment.objects.create(
        number="CP-1002", customer=customer, date=date(2024, 1, 1), method=PaymentMethod.CHECK,
        amount=Decimal("1.00"), deposited_to=Account.objects.get(name=UNDEPOSITED),
        created_by=replay.user)
    with transaction.atomic():
        with use_number(DocType.PMT, "CP-1002"):
            with pytest.raises(SalesError, match="CP-1002 is already used."):
                next_number(DocType.PMT)
            assert next_number(DocType.PMT) == "PMT-00001"  # the refused call consumed the override


def test_master_accounts_numbered_and_settings_configured(replay):
    assert InventorySettings.load().is_configured
    assert SalesSettings.load().is_configured
    assert PurchasingSettings.load().is_configured
    assert replay.accounts["Accounts Receivable"].number == "1100"
    assert replay.accounts["Accounts Payable"].number == "2000"
    assert SalesSettings.load().undeposited_account == replay.accounts[UNDEPOSITED]
    assert InventorySettings.load().opening_equity_account.name == "Opening Balance Offset"
    assert Account.objects.count() == 22
    assert replay.accounts["Sales"].number == "4000" or replay.accounts["Sales"].number.startswith("40")


def test_bank_accounts_created_by_kind(replay):
    kinds = {b.ledger_account.name: b.kind for b in BankAccount.objects.all()}
    assert kinds == {
        BANK: AccountKind.CHECKING, CARD: AccountKind.CARD,
        LOC_CHASE: AccountKind.LOAN, LOC_HEADWAY: AccountKind.LOAN,
    }
    assert "Petty Cash" not in kinds and UNDEPOSITED not in kinds


def test_items_customers_vendors_and_zoho_records(replay):
    assert set(replay.items) >= {"WID-A", "WID-B", "GAD-C", "BOLT-D", "WRAP", "SHIP", "ADJ", "TAX"}
    gadget = replay.items["GAD-C"]
    assert gadget.is_stock and gadget.costing_method == "FIFO"
    assert gadget.cogs_account.name == "Cost of Goods Sold" and gadget.vendor_part_number == "PW-GAD-C"
    assert gadget.sale_price == Decimal("50.00") and gadget.purchase_cost == Decimal("28.00")
    assert gadget.description == "Gadget, large" and gadget.category.name == "Hardware"
    wrap = replay.items["WRAP"]
    assert not wrap.is_stock and wrap.inventory_account is None and wrap.costing_method == ""
    ship, adj, tax = replay.items["SHIP"], replay.items["ADJ"], replay.items["TAX"]
    assert (ship.name, ship.income_account.name) == ("Shipping charge", "Shipping Charge")
    assert (adj.name, adj.income_account.name) == ("Adjustment", "Sales")
    assert (tax.name, tax.income_account.name) == ("Sales tax (Zoho)", "Tax Payable")
    assert not ship.is_stock and not adj.is_stock and not tax.is_stock

    assert set(replay.customers) == {"Acme Hardware", "Birch Lane Supply", "Cedar Ridge Outfitters"}
    acme = replay.customers["Acme Hardware"]
    assert acme.terms == Terms.NET_30 and acme.email == "ap@acme.example"
    assert acme.billing_address == "12 Main St\nAustin, TX 78701"
    assert "Dock 2" in acme.ship_to_address
    assert replay.customers["Cedar Ridge Outfitters"].terms == Terms.DUE_ON_RECEIPT
    assert set(replay.vendors) == {"Mr. Joseph Cordon", "Pioneer Wholesale", "Summit Packaging"}
    assert replay.vendors["Mr. Joseph Cordon"].terms == Terms.DUE_ON_RECEIPT
    from handling.models import HandlingSettings
    assert HandlingSettings.load().vendor_id is None

    for kind, count in (("account", 22), ("item", 5), ("customer", 3), ("vendor", 3), ("bank_account", 4)):
        assert ZohoRecord.objects.filter(kind=kind).count() == count
    record = ZohoRecord.objects.get(kind="customer", zoho_id="4800000000001")
    assert record.target == acme
    assert replay.warnings == []


def test_opening_stock_posted_at_2020_02_01(replay):
    gadget = replay.items["GAD-C"]
    movement = StockMovement.objects.get(item=gadget)
    assert movement.kind == MovementKind.OPENING and movement.date == date(2020, 2, 1)
    assert movement.quantity == 40 and movement.unit_cost == Decimal("25.0000")
    assert movement.total_cost == Decimal("1000.00")
    assert StockMovement.objects.count() == 1
    entry = movement.journal_entry
    lines = {l.account.name: l.debit - l.credit for l in JournalLine.objects.in_ledger().filter(entry=entry)}
    assert lines == {"Inventory Asset": Decimal("1000.00"), "Opening Balance Offset": Decimal("-1000.00")}
    assert ZohoRecord.objects.get(kind="opening_stock", zoho_id="4700000000003").target == movement

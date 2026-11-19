"""purchasing 0007 and sales 0012: imported Zoho adjustments become flagged adjustment lines. Flags only."""
import importlib
from datetime import date
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from items.models import ItemType
from ledger.models import AccountSubtype, AccountType, JournalLine
from purchasing.balances import bill_total
from purchasing.models import PurchasingSettings
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.balances import invoice_total
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from tests.conftest import _account

D = Decimal
DAY = date(2026, 10, 5)

bills_migration = importlib.import_module("purchasing.migrations.0007_convert_zoho_adjustments")
invoices_migration = importlib.import_module("sales.migrations.0012_convert_zoho_adjustments")


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


def _ledger():
    return sorted(JournalLine.objects.values_list("entry_id", "account_id", "debit", "credit"))


def _bill(vendor, user, expense, *lines):
    return create_bill(vendor=vendor, date=DAY, vendor_invoice_number="", user=user, allow_negative_adjustment=True,
                       expense_lines=[BillExpenseSpec(expense, D(a), desc) for desc, a in lines])


def test_single_bill_adjustment_line_is_flagged(setup, vendor, admin_user, expense):
    one = _bill(vendor, admin_user, expense, ("Freight", "100.00"), ("Adjustment", "-7.50"))
    two = _bill(vendor, admin_user, expense, ("Freight", "100.00"), ("Adjustment", "-1.00"), ("Adjustment", "2.00"))
    plain = _bill(vendor, admin_user, expense, ("Adjustment fee", "5.00"))
    totals, ledger = [bill_total(b) for b in (one, two, plain)], _ledger()

    bills_migration.convert(django_apps)

    flagged = one.expense_lines.get(is_adjustment=True)
    assert (flagged.amount, flagged.spreads_to_orders) == (D("-7.50"), False)
    assert not two.expense_lines.filter(is_adjustment=True).exists()
    assert not plain.expense_lines.filter(is_adjustment=True).exists()
    assert [bill_total(b) for b in (one, two, plain)] == totals
    assert _ledger() == ledger


def test_default_adjustment_account_seeded_from_5010(setup):
    wholesale = _account("5010", "Wholesale Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
    bills_migration.convert(django_apps)
    assert PurchasingSettings.load().default_adjustment_account == wholesale


def test_default_adjustment_account_not_overwritten(setup, expense):
    _account("5010", "Wholesale Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
    settings = PurchasingSettings.load()
    settings.default_adjustment_account = expense
    settings.save()
    bills_migration.convert(django_apps)
    assert PurchasingSettings.load().default_adjustment_account == expense


def test_no_5010_leaves_the_setting_blank(setup):
    bills_migration.convert(django_apps)
    assert PurchasingSettings.load().default_adjustment_account is None


def test_single_invoice_adjustment_line_is_flagged(setup, customer, admin_user, make_item, revenue):
    other = _account("4050", "Wholesale Sales", AccountType.INCOME, AccountSubtype.INCOME)
    non_stock = dict(item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)
    svc = make_item("SVC", **non_stock)
    adj = make_item("ADJ", **non_stock)
    adj_copy = make_item("ADJ-4050", income_account=other, **non_stock)

    def invoice(*lines):
        return create_invoice(customer=customer, date=DAY, user=admin_user, allow_negative_adjustment=True,
                              lines=[InvoiceLineSpec(item, 1, D(p)) for item, p in lines])

    one = invoice((svc, "50.00"), (adj, "-0.25"))
    copy = invoice((svc, "50.00"), (adj_copy, "3.00"))
    two = invoice((svc, "50.00"), (adj, "1.00"), (adj_copy, "1.00"))
    none = invoice((svc, "50.00"))
    totals, ledger = [invoice_total(i) for i in (one, copy, two, none)], _ledger()

    invoices_migration.convert(django_apps)

    assert one.lines.get(is_adjustment=True).item == adj
    assert copy.lines.get(is_adjustment=True).item == adj_copy
    assert not two.lines.filter(is_adjustment=True).exists()
    assert not none.lines.filter(is_adjustment=True).exists()
    assert [invoice_total(i) for i in (one, copy, two, none)] == totals
    assert _ledger() == ledger


def test_unconvert_clears_flags(setup, vendor, admin_user, expense):
    bill = _bill(vendor, admin_user, expense, ("Freight", "100.00"), ("Adjustment", "-7.50"))
    bills_migration.convert(django_apps)
    bills_migration.unconvert(django_apps)
    assert not bill.expense_lines.filter(is_adjustment=True).exists()

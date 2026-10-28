from decimal import Decimal

import pytest

from banking.models import AccountKind
from contacts.models import Terms
from ledger.models import AccountSubtype, AccountType
from migration.zoho.mapping import (
    account_plan, address, bank_kind, customer_method, line_price, money, terms, vendor_method,
)
from purchasing.models import VendorPaymentMethod
from sales.models import PaymentMethod


def _acct(name, ztype, status="Active"):
    return {"Account Name": name, "Account Type": ztype, "Account Status": status}


def test_money_parses_zoho_forms():
    assert money("1,234.5") == Decimal("1234.50")
    assert money("0.000") == Decimal("0.00")
    assert money("") == Decimal("0.00")


def test_account_plan_numbers_by_type_and_name():
    plan = {p.name: p for p in account_plan([
        _acct("Zeta Bank", "Bank"), _acct("Alpha Bank", "Bank", "Inactive"),
        _acct("Petty Cash", "Cash"),
        _acct("Accounts Receivable", "Accounts Receivable"),
        _acct("Accounts Payable", "Accounts Payable"),
        _acct("Visa", "Credit Card"), _acct("Sales", "Income"), _acct("Rent", "Expense"),
    ])}
    assert plan["Alpha Bank"].number == "1000" and not plan["Alpha Bank"].is_active
    assert plan["Petty Cash"].number == "1010" and plan["Zeta Bank"].number == "1020"
    assert plan["Accounts Receivable"].number == "1100"
    assert plan["Accounts Payable"].number == "2000"
    assert plan["Visa"].number == "2100" and plan["Visa"].subtype == AccountSubtype.CREDIT_CARD
    assert plan["Sales"].number == "4000" and plan["Sales"].type == AccountType.INCOME
    assert plan["Rent"].number == "6000" and plan["Rent"].is_active


def test_account_plan_overflow_raises():
    rows = [_acct(f"Eq {i:03d}", "Equity") for i in range(11)]
    with pytest.raises(ValueError):
        account_plan(rows)
    assert len(account_plan(rows[:10])) == 10


def test_bank_kind():
    assert bank_kind("Chase", "Bank") == AccountKind.CHECKING
    assert bank_kind("Drawer", "Cash") == AccountKind.CHECKING
    assert bank_kind("Undeposited Funds", "Cash") is None
    assert bank_kind("Petty Cash", "Cash") is None
    assert bank_kind("Line Of Credit - Chase", "Credit Card") == AccountKind.LOAN
    assert bank_kind("Visa", "Credit Card") == AccountKind.CARD
    assert bank_kind("Line Of Credit - Headway", "Other Liability") == AccountKind.LOAN
    assert bank_kind("Mortgage", "Other Liability") is None
    assert bank_kind("Sales", "Income") is None


def test_terms_labels():
    assert terms("Due on Receipt") == Terms.DUE_ON_RECEIPT
    assert terms("Due On Receipt") == Terms.DUE_ON_RECEIPT
    assert terms("Net 15") == Terms.NET_15
    assert terms("Net 30") == Terms.NET_30
    assert terms("Net 60") == Terms.NET_60
    assert terms("Net 7") == Terms.NET_15
    assert terms("Net 40") == Terms.NET_45
    assert terms("Due end of the month") == Terms.NET_30
    assert terms("") == Terms.NET_30


def test_address_joins_non_blank_parts():
    row = {"Billing Address": "12 Main St", "Billing Street2": "Dock 2", "Billing City": "Austin",
           "Billing State": "TX", "Billing Code": "78701"}
    assert address(row, "Billing") == "12 Main St\nDock 2\nAustin, TX 78701"
    assert address({"Shipping Address": "9 Elm", "Shipping City": "Reno"}, "Shipping") == "9 Elm\nReno"
    assert address({}, "Billing") == ""


def test_line_price_discount():
    assert line_price(Decimal("10.00"), 3) == (Decimal("3.3333"), Decimal("0.00"))
    assert line_price(Decimal("1000.00"), 300) == (Decimal("3.3333"), Decimal("0.01"))
    assert line_price(Decimal("0.00"), 2) == (Decimal("0.0000"), Decimal("0.00"))


def test_customer_method():
    assert customer_method("Check", "Undeposited Funds") == PaymentMethod.CHECK
    assert customer_method("Cash", "Undeposited Funds") == PaymentMethod.CASH
    assert customer_method("Bank Transfer", "Undeposited Funds") == PaymentMethod.CASH
    assert customer_method("Check", "Chase Checking") == PaymentMethod.ACH


def test_vendor_method():
    assert vendor_method("Check", AccountSubtype.CREDIT_CARD) == VendorPaymentMethod.CARD
    assert vendor_method("Check", AccountSubtype.BANK) == VendorPaymentMethod.CHECK
    assert vendor_method("Bank Transfer", AccountSubtype.BANK) == VendorPaymentMethod.ACH
    assert vendor_method("Cash", None) == VendorPaymentMethod.ACH


def test_custom_terms_from_the_due_date_gap():
    assert terms("Custom", "2020-05-05", "2020-06-05") == Terms.NET_45  # 31 days: the next longer terms
    assert terms("Custom", "2020-05-05", "2020-06-04") == Terms.NET_30
    assert terms("Custom", "2020-05-05", "2020-05-05") == Terms.DUE_ON_RECEIPT
    assert terms("Custom") == Terms.NET_30


def test_money_reads_a_usd_prefix_and_refuses_other_currencies():
    assert money("USD 2.99") == Decimal("2.99")
    assert money("USD 1,250.00") == Decimal("1250.00")
    with pytest.raises(ValueError, match="Only US dollar amounts"):
        money("EUR 5.00")

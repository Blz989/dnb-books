from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer, Vendor
from items.models import ItemType
from ledger.posting import LineSpec, post_entry
from ledger.sales_reports import (
    purchases_by_item, purchases_by_vendor, sales_by_customer, sales_by_item,
)
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill, edit_bill, void_bill
from purchasing.posting.credits import AllowanceLineSpec, create_allowance_credit
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, void_invoice
from sales.posting.receipts import CreditLineSpec, create_credit_note

D = Decimal
NOV = (date(2026, 11, 1), date(2026, 11, 30))
DEC = (date(2026, 12, 1), date(2026, 12, 31))
BOTH = (date(2026, 11, 1), date(2026, 12, 31))


@pytest.fixture
def setup(inv_settings, sales_settings, purchasing_settings):
    return sales_settings


@pytest.fixture
def svc(setup, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None,
                     inventory_account=None)


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None,
                     inventory_account=None)


@pytest.fixture
def other_customer(db):
    return Customer.objects.create(name="Bolt Barn")


def invoice(customer, user, day, *lines):
    return create_invoice(customer=customer, date=day, user=user,
                          lines=[InvoiceLineSpec(item, qty, D(price)) for item, qty, price in lines])


# ---- sales by customer ----

def test_sales_by_customer_groups_and_ties_to_pnl(admin_user, customer, other_customer, svc, gadget, cash, revenue):
    invoice(customer, admin_user, date(2026, 11, 3), (svc, 2, "100.00"), (gadget, 1, "50.00"))
    invoice(customer, admin_user, date(2026, 11, 9), (svc, 1, "100.00"))
    invoice(other_customer, admin_user, date(2026, 11, 12), (gadget, 4, "25.00"))
    create_credit_note(customer=customer, date=date(2026, 11, 20), user=admin_user, reason="Damaged",
                       lines=[CreditLineSpec("Damaged", D("30.00"))])
    post_entry(date=date(2026, 11, 25), user=admin_user, memo="Interest",
               lines=[LineSpec(cash, debit=D("7.00")), LineSpec(revenue, credit=D("7.00"))])

    r = sales_by_customer(*NOV)
    rows = {row.customer.name: row for row in r.rows}
    assert [row.customer.name for row in r.rows] == ["Acme Builders", "Bolt Barn"]
    acme = rows["Acme Builders"]
    assert (acme.invoices, acme.sales, acme.credits, acme.net) == (2, D("350.00"), D("30.00"), D("320.00"))
    assert (rows["Bolt Barn"].invoices, rows["Bolt Barn"].net) == (1, D("100.00"))
    assert r.total.net == D("420.00")
    assert r.other_income == D("7.00")
    assert r.pnl_income == D("427.00")
    assert r.difference == D("0.00")


def test_sales_by_customer_void_in_later_month(admin_user, customer, svc):
    inv = invoice(customer, admin_user, date(2026, 11, 10), (svc, 1, "200.00"))
    void_invoice(inv, user=admin_user, reason="Wrong customer", date=date(2026, 12, 2))
    assert sales_by_customer(*NOV).total.net == D("200.00")
    dec = sales_by_customer(*DEC)
    assert dec.total.net == D("-200.00") and dec.rows[0].invoices == 0
    assert sales_by_customer(*BOTH).total.net == D("0.00")
    for rng in (NOV, DEC, BOTH):
        assert sales_by_customer(*rng).difference == D("0.00")


def test_sales_by_customer_empty(setup):
    r = sales_by_customer(*NOV)
    assert r.rows == [] and r.total.net == D("0.00") and r.difference == D("0.00")


# ---- purchases by vendor ----

def test_purchases_by_vendor(admin_user, vendor, expense, setup):
    other = Vendor.objects.create(name="Ohio Freight")
    create_bill(vendor=vendor, date=date(2026, 11, 2), vendor_invoice_number="A1", user=admin_user,
                expense_lines=[BillExpenseSpec(expense, D("120.00"))])
    b = create_bill(vendor=other, date=date(2026, 11, 4), vendor_invoice_number="F1", user=admin_user,
                    expense_lines=[BillExpenseSpec(expense, D("80.00"))])
    create_allowance_credit(vendor=vendor, date=date(2026, 11, 6), user=admin_user, reason="Short",
                            lines=[AllowanceLineSpec(expense, D("20.00"))])
    void_bill(b, user=admin_user, reason="Duplicate", date=date(2026, 12, 1))

    r = purchases_by_vendor(*NOV)
    rows = {row.vendor.name: row for row in r.rows}
    assert (rows[vendor.name].bills, rows[vendor.name].purchases, rows[vendor.name].credits,
            rows[vendor.name].net) == (1, D("120.00"), D("20.00"), D("100.00"))
    assert rows["Ohio Freight"].net == D("80.00")
    assert r.total.net == D("180.00") and r.ap_total == D("180.00") and r.difference == D("0.00")
    assert purchases_by_vendor(*DEC).total.net == D("-80.00")
    assert purchases_by_vendor(*BOTH).total.net == D("100.00")


# ---- sales by item ----

def test_sales_by_item_rows_and_tie_out(admin_user, customer, other_customer, svc, gadget):
    invoice(customer, admin_user, date(2026, 11, 3), (svc, 2, "100.00"), (gadget, 1, "50.00"))
    invoice(other_customer, admin_user, date(2026, 11, 12), (gadget, 4, "25.00"))
    r = sales_by_item(*NOV)
    rows = {row.item.sku: row for row in r.rows}
    assert (rows["SVC"].quantity, rows["SVC"].amount, rows["SVC"].average) == (2, D("200.00"), D("100.00"))
    assert (rows["GADGET"].quantity, rows["GADGET"].amount, rows["GADGET"].average) == (5, D("150.00"), D("30.00"))
    assert r.total == D("350.00") and r.ledger_total == D("350.00") and r.difference == D("0.00")

    only = sales_by_item(*NOV, customer=other_customer)
    assert [(row.item.sku, row.quantity) for row in only.rows] == [("GADGET", 4)]
    assert only.difference == D("0.00")


def test_sales_by_item_void_split_across_ranges(admin_user, customer, svc):
    inv = invoice(customer, admin_user, date(2026, 11, 10), (svc, 3, "10.00"))
    void_invoice(inv, user=admin_user, reason="Cancelled", date=date(2026, 12, 2))
    nov, dec, both = sales_by_item(*NOV), sales_by_item(*DEC), sales_by_item(*BOTH)
    assert (nov.rows[0].quantity, nov.total) == (3, D("30.00"))
    assert (dec.rows[0].quantity, dec.total) == (-3, D("-30.00"))
    assert both.rows == [] or both.total == D("0.00")
    for r in (nov, dec, both):
        assert r.difference == D("0.00")


def test_sales_by_item_after_edit(admin_user, customer, svc):
    inv = invoice(customer, admin_user, date(2026, 11, 10), (svc, 3, "10.00"))
    edit_invoice(inv, user=admin_user, date=date(2026, 11, 10), terms=None, ship_to="", notes="",
                 lines=[InvoiceLineSpec(svc, 5, D("10.00"))], reason="More")
    r = sales_by_item(*NOV)
    assert (r.rows[0].quantity, r.total, r.difference) == (5, D("50.00"), D("0.00"))


# ---- purchases by item ----

def test_purchases_by_item(admin_user, vendor, expense, freight, setup, make_item):
    widget = make_item("WIDGET", purchase_cost="0.5000")
    b = create_bill(vendor=vendor, date=date(2026, 11, 2), vendor_invoice_number="A1", user=admin_user,
                    item_lines=[BillItemSpec(widget, 100, D("0.50"))],
                    expense_lines=[BillExpenseSpec(freight, D("12.00"))])
    create_bill(vendor=vendor, date=date(2026, 11, 9), vendor_invoice_number="A2", user=admin_user,
                item_lines=[BillItemSpec(widget, 300, D("0.70"))])
    r = purchases_by_item(*NOV)
    item_row = next(row for row in r.rows if row.item)
    acct_row = next(row for row in r.rows if row.account)
    assert (item_row.quantity, item_row.amount, item_row.average) == (400, D("260.00"), D("0.65"))
    assert (acct_row.account, acct_row.quantity, acct_row.amount) == (freight, None, D("12.00"))
    assert r.total == D("272.00") and r.ledger_total == D("272.00") and r.difference == D("0.00")

    edit_bill(b, user=admin_user, date=date(2026, 11, 2), vendor_invoice_number="A1", terms=None, notes="",
              item_lines=[BillItemSpec(widget, 100, D("0.50"))], expense_lines=[], reason="No freight")
    r = purchases_by_item(*NOV)
    assert r.total == D("260.00") and r.difference == D("0.00")
    assert purchases_by_item(*NOV, vendor=Vendor.objects.create(name="Nobody")).rows == []


# ---- views ----

@pytest.mark.parametrize("slug,text", [
    ("sales-by-customer", "Sales by customer"), ("sales-by-item", "Sales by item"),
    ("purchases-by-vendor", "Purchases by vendor"), ("purchases-by-item", "Purchases by item"),
])
def test_report_pages_csv_and_catalog(client, standard_user, setup, slug, text):
    client.force_login(standard_user)
    html = client.get(f"/reports/{slug}/?start=2026-11-01&end=2026-11-30").content.decode()
    assert text in html and "Difference" in html
    resp = client.get(f"/reports/{slug}/?start=2026-11-01&end=2026-11-30&format=csv")
    assert resp["Content-Type"].startswith("text/csv")
    assert "Sales and purchases" in client.get("/reports/").content.decode()

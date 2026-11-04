"""Cash-basis P&L, SKU series, reorder points and Low stock, purchase order PDF, and the audit log screen."""
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from contacts.models import Terms
from items.models import InventorySettings, Item, ItemType, MovementKind
from items.sku import advance_past, next_sku
from items.stock import StockLine, record_movements
from ledger.cash_basis import profit_and_loss_cash
from ledger.models import JournalLine
from ledger.posting import LineSpec, post_entry
from ledger.reports import profit_and_loss
from purchasing.models import VendorPaymentMethod
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.orders import POLineSpec, create_po
from purchasing.posting.payments import pay_vendor
from sales.models import PaymentMethod
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment, void_payment

D = Decimal


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def income(report) -> Decimal:
    return sum((r.amount for r in report.income), D("0"))


def expenses(report) -> Decimal:
    return sum((r.amount for r in report.operating_expenses), D("0"))


# ---- cash-basis P&L ----

def test_income_counts_when_paid_in_proportion(admin_user, customer, service, cash):
    inv = create_invoice(customer=customer, date=date(2026, 1, 10), user=admin_user, terms=Terms.NET_30,
                         lines=[InvoiceLineSpec(service, 1, D("1000.00"))])
    receive_payment(customer=customer, date=date(2026, 2, 5), method=PaymentMethod.ACH, amount=D("400.00"),
                    user=admin_user, bank_account=cash, applications=[(inv, D("400.00"))])
    jan, feb = (date(2026, 1, 1), date(2026, 1, 31)), (date(2026, 2, 1), date(2026, 2, 28))
    assert income(profit_and_loss(*jan)) == D("1000.00")
    assert income(profit_and_loss_cash(*jan)) == D("0")
    assert income(profit_and_loss_cash(*feb)) == D("400.00")


def test_voided_payment_counts_back_out_when_voided(admin_user, customer, service, cash):
    inv = create_invoice(customer=customer, date=date(2026, 1, 10), user=admin_user,
                         lines=[InvoiceLineSpec(service, 1, D("500.00"))])
    pmt = receive_payment(customer=customer, date=date(2026, 2, 5), method=PaymentMethod.ACH, amount=D("500.00"),
                          user=admin_user, bank_account=cash, applications=[(inv, D("500.00"))])
    void_payment(pmt, user=admin_user, reason="Bounced", date=date(2026, 3, 3))
    assert income(profit_and_loss_cash(date(2026, 2, 1), date(2026, 2, 28))) == D("500.00")
    assert income(profit_and_loss_cash(date(2026, 3, 1), date(2026, 3, 31))) == D("-500.00")
    assert income(profit_and_loss_cash(date(2026, 1, 1), date(2026, 12, 31))) == D("0")


def test_bill_expense_counts_when_paid(admin_user, vendor, expense, cash, purchasing_settings):
    bill = create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="R-1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("300.00"))])
    pay_vendor(vendor=vendor, date=date(2026, 2, 1), method=VendorPaymentMethod.ACH, amount=D("300.00"),
               paid_from=cash, user=admin_user, applications=[(bill, D("300.00"))])
    assert expenses(profit_and_loss_cash(date(2026, 1, 1), date(2026, 1, 31))) == D("0")
    assert expenses(profit_and_loss_cash(date(2026, 2, 1), date(2026, 2, 28))) == D("300.00")


def test_journal_entries_count_as_posted(admin_user, cash, expense):
    post_entry(date=date(2026, 1, 20), user=admin_user, memo="Depreciation",
               lines=[LineSpec(expense, debit=D("75.00")), LineSpec(cash, credit=D("75.00"))])
    assert expenses(profit_and_loss_cash(date(2026, 1, 1), date(2026, 1, 31))) == D("75.00")


def test_cash_and_accrual_agree_once_everything_is_paid(admin_user, customer, service, cash, vendor, expense,
                                                         purchasing_settings):
    inv = create_invoice(customer=customer, date=date(2026, 1, 10), user=admin_user,
                         lines=[InvoiceLineSpec(service, 3, D("333.33"))])
    receive_payment(customer=customer, date=date(2026, 2, 1), method=PaymentMethod.ACH, amount=D("999.99"),
                    user=admin_user, bank_account=cash, applications=[(inv, D("999.99"))])
    bill = create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="R-2", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("120.00"))])
    pay_vendor(vendor=vendor, date=date(2026, 3, 1), method=VendorPaymentMethod.ACH, amount=D("120.00"),
               paid_from=cash, user=admin_user, applications=[(bill, D("120.00"))])
    year = (date(2026, 1, 1), date(2026, 12, 31))
    assert profit_and_loss_cash(*year).net_income == profit_and_loss(*year).net_income == D("879.99")


def test_profit_and_loss_basis_switch(adm, admin_user, customer, service, cash):
    create_invoice(customer=customer, date=date(2026, 1, 10), user=admin_user,
                   lines=[InvoiceLineSpec(service, 1, D("250.00"))])
    accrual = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31").content.decode()
    cash_html = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31&basis=cash").content.decode()
    assert "accrual basis" in accrual and "250.00" in accrual
    assert "cash basis" in cash_html and "Cash basis: income counts when customers pay" in cash_html
    assert "250.00" not in cash_html.split("Net income")[0].split("<tbody>")[-1]
    csv = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31&basis=cash&format=csv")
    assert "profit-and-loss-cash-" in csv["Content-Disposition"]
    compared = adm.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31&basis=cash&compare=1")
    assert "Jan 1 – Jan 31, 2025" in compared.content.decode()


# ---- SKU series ----

@pytest.fixture
def series(inv_settings):
    s = InventorySettings.load()
    s.sku_prefix, s.sku_next, s.sku_digits = "DNB-", 7, 4
    s.save()
    return s


def test_no_series_means_no_suggestion(inv_settings, adm):
    assert next_sku() == ""
    assert 'name="sku"' in adm.get("/items/new/").content.decode()
    assert "Next in your SKU series" not in adm.get("/items/new/").content.decode()


def test_new_item_form_offers_next_sku(series, adm):
    html = adm.get("/items/new/").content.decode()
    assert 'value="DNB-0007"' in html and "Next in your SKU series" in html


def test_saving_moves_the_counter_forward_never_back(series, make_item):
    advance_past("DNB-0007")
    assert InventorySettings.load().sku_next == 8
    advance_past("dnb-0020")  # case doesn't matter
    assert InventorySettings.load().sku_next == 21
    advance_past("DNB-0003")
    advance_past("OTHER-99")
    assert InventorySettings.load().sku_next == 21


def test_next_sku_skips_taken(series, make_item):
    make_item("DNB-0007")
    make_item("DNB-0008")
    assert next_sku() == "DNB-0009"


def test_item_form_save_advances_series(series, adm):
    r = adm.post("/items/new/", {"sku": "DNB-0007", "name": "Hex bolt", "item_type": "STOCK", "unit": "each",
                                 "sale_price": "1", "purchase_cost": "0.5", "costing_method": "FIFO",
                                 "income_account": series.default_income_account_id,
                                 "cogs_account": series.default_cogs_account_id,
                                 "inventory_account": series.default_inventory_account_id})
    assert r.status_code == 302 and InventorySettings.load().sku_next == 8


def test_sku_series_settings(adm, inv_settings):
    r = adm.post("/items/settings/", {"action": "sku", "sku_prefix": "PNT-", "sku_next": "42", "sku_digits": "3"})
    assert r.status_code == 302
    s = InventorySettings.load()
    assert (s.sku_prefix, s.sku_next, s.sku_digits) == ("PNT-", 42, 3)
    assert "PNT-042" in adm.get("/items/settings/").content.decode()
    bad = adm.post("/items/settings/", {"action": "sku", "sku_prefix": "a b", "sku_next": "0", "sku_digits": "9"})
    html = bad.content.decode()
    assert "The base SKU can use letters" in html and "Enter a next number of 1 or more." in html
    assert "Enter 1 to 8 digits." in html


def test_accounts_form_still_saves_without_action(adm, inv_settings):
    data = {f: getattr(inv_settings, f"{f}_id") for f in InventorySettings._ACCOUNT_FIELDS}
    assert adm.post("/items/settings/", data).status_code == 302


# ---- reorder points ----

def test_low_stock_report(adm, admin_user, inv_settings, make_item, vendor, purchasing_settings):
    low = make_item("LOW", reorder_point=10)
    ok = make_item("OK", reorder_point=2)
    make_item("UNTRACKED")
    record_movements(kind=MovementKind.OPENING, date=date(2026, 1, 2), user=admin_user,
                     lines=[StockLine(low, 3, unit_cost=D("1.00")), StockLine(ok, 5, unit_cost=D("1.00"))])
    create_po(vendor=vendor, date=date(2026, 1, 3), user=admin_user, lines=[POLineSpec(low, 4, D("1.00"))])
    from items.reports import low_stock

    rows = low_stock()
    assert [r.item.sku for r in rows] == ["LOW"]
    assert (rows[0].on_hand, rows[0].on_order, rows[0].short_by) == (3, 4, 3)
    html = adm.get("/reports/low-stock/").content.decode()
    assert "1 item to reorder." in html and "Download PDF" in html and "Download CSV" in html
    assert "Items to reorder" in adm.get("/").content.decode()


def test_reorder_point_cleared_for_non_stock(adm, inv_settings):
    adm.post("/items/new/", {"sku": "SVC-1", "name": "Delivery", "item_type": "NON_STOCK", "unit": "each",
                             "sale_price": "10", "purchase_cost": "0", "reorder_point": "5",
                             "income_account": inv_settings.default_income_account_id})
    assert Item.objects.get(sku="SVC-1").reorder_point is None


def test_negative_stock_report_keeps_pdf_button(adm, db):
    html = adm.get("/reports/negative-stock/").content.decode()
    assert "Download CSV" in html and "Download PDF" in html


# ---- purchase order PDF ----

def test_po_pdf(adm, admin_user, vendor, make_item, sales_settings, purchasing_settings):
    from purchasing.pdf import render_po_html

    item = make_item("BOLT", vendor_part_number="VP-9")
    po = create_po(vendor=vendor, date=date(2026, 1, 3), user=admin_user, lines=[POLineSpec(item, 12, D("0.75"))])
    html = render_po_html(po)
    assert "PURCHASE ORDER" in html and po.number in html and vendor.name in html
    assert "VP-9" in html and "9.00" in html and "DNB Supply" in html  # ships to the company
    r = adm.get(f"/purchasing/orders/{po.number}/pdf/")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert f'/purchasing/orders/{po.number}/pdf/' in adm.get(f"/purchasing/orders/{po.number}/").content.decode()


# ---- audit log ----

def test_audit_log_lists_changes(adm, admin_user, cash):
    cash.name = "Main checking"
    cash.save()
    html = adm.get("/settings/audit-log/").content.decode()
    assert "Main checking" in html and "Changed" in html and "Cash" in html


def test_audit_log_filters(adm, cash, customer):
    from django.contrib.contenttypes.models import ContentType

    from contacts.models import Customer

    ct = ContentType.objects.get_for_model(Customer)
    html = adm.get(f"/settings/audit-log/?type={ct.pk}").content.decode()
    assert "Acme Builders" in html and "1000 Cash" not in html
    assert "No changes match these filters." in adm.get("/settings/audit-log/?q=nothing-like-this").content.decode()


def test_audit_log_masks_tax_ids(adm, vendor):
    vendor.tax_id = "123456789"
    vendor.save()
    html = adm.get("/settings/audit-log/").content.decode()
    assert "123456789" not in html


def test_audit_log_is_admin_only(std, db):
    assert std.get("/settings/audit-log/").status_code == 403


def test_audit_log_hides_journal_lines_unless_asked(adm, posted_entry):
    from django.contrib.contenttypes.models import ContentType

    html = adm.get("/settings/audit-log/").content.decode()
    assert posted_entry.display_number in html and "JournalLine object" not in html
    ct = ContentType.objects.get_for_model(JournalLine)
    assert "JournalLine object" in adm.get(f"/settings/audit-log/?type={ct.pk}").content.decode()

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Terms, Vendor
from ledger.models import Account, AccountSubtype, AccountType
from purchasing.models import VendorPaymentMethod as M
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, bill_from_po, create_bill
from purchasing.posting.orders import POLineSpec, create_po
from purchasing.posting.payments import PayLine, pay_bills, pay_vendor, void_vendor_payment
from purchasing.reports import ap_aging, bills_due, open_purchase_orders, summary_1099, vendor_balances

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(purchasing_settings, expense):
    return purchasing_settings


@pytest.fixture
def zed(db):
    return Vendor.objects.create(name="Zed Parts", terms=Terms.NET_15)


def bill(vendor, admin_user, expense, amount, on, ref="B", terms=None):
    return create_bill(vendor=vendor, date=on, vendor_invoice_number=f"{ref}-{on.isoformat()}-{amount}", user=admin_user,
                       terms=terms, expense_lines=[BillExpenseSpec(expense, D(amount))])


# ---- A/P aging ----

@pytest.mark.parametrize("as_of, bucket", [
    (d(10, 30), "current"), (d(10, 31), "current"), (d(11, 1), "d1_30"), (d(11, 30), "d1_30"),
    (d(12, 1), "d31_60"), (d(12, 30), "d31_60"), (d(12, 31), "d61_90"),
])
def test_bucket_edges(setup, vendor, admin_user, expense, as_of, bucket):
    bill(vendor, admin_user, expense, "100.00", d(10, 1))  # NET_30, due Oct 31
    report = ap_aging(as_of)
    assert getattr(report.rows[0], bucket) == D("100.00")


def test_over_90(setup, vendor, admin_user, expense):
    bill(vendor, admin_user, expense, "100.00", d(1, 1))
    report = ap_aging(d(6, 1))
    assert report.rows[0].over_90 == D("100.00")


def test_aging_total_equals_ap_account(setup, vendor, zed, admin_user, expense, cash):
    a = bill(vendor, admin_user, expense, "100.00", d(10, 1))
    bill(zed, admin_user, expense, "50.00", d(10, 5))
    pay_vendor(vendor=vendor, date=d(10, 10), method=M.ACH, amount=D("130.00"), paid_from=cash, user=admin_user,
               applications=[(a, D("100.00"))])
    report = ap_aging(d(10, 31))
    assert [r.vendor.name for r in report.rows] == ["Acme Supply", "Zed Parts"]
    assert report.rows[0].unapplied_credits == D("-30.00")
    assert report.totals.total == D("20.00") == report.ledger_balance
    assert report.difference == D("0.00")


def test_aging_as_of_before_a_payment(setup, vendor, admin_user, expense, cash):
    a = bill(vendor, admin_user, expense, "100.00", d(10, 1))
    pay_vendor(vendor=vendor, date=d(10, 10), method=M.ACH, amount=D("100.00"), paid_from=cash, user=admin_user,
               applications=[(a, D("100.00"))])
    assert ap_aging(d(10, 9)).totals.total == D("100.00")
    assert ap_aging(d(10, 10)).totals.total == D("0.00")


def test_difference_shows_when_ledger_disagrees(setup, vendor, admin_user, expense, ap):
    from ledger.posting import LineSpec, post_entry

    bill(vendor, admin_user, expense, "100.00", d(10, 1))
    post_entry(date=d(10, 2), user=admin_user, lines=[LineSpec(expense, debit=D("7.00")), LineSpec(ap, credit=D("7.00"))])
    report = ap_aging(d(10, 31))
    assert (report.totals.total, report.ledger_balance, report.difference) == (D("100.00"), D("107.00"), D("-7.00"))


# ---- bills due, balances, open POs ----

def test_bills_due_through_a_date(setup, vendor, zed, admin_user, expense, cash):
    a = bill(vendor, admin_user, expense, "100.00", d(10, 1))      # due Oct 31
    b = bill(zed, admin_user, expense, "50.00", d(10, 20))         # due Nov 4
    paid = bill(vendor, admin_user, expense, "10.00", d(10, 2), ref="P")
    pay_bills(date=d(10, 3), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(paid, D("10.00"))])
    rows = bills_due(d(11, 4))
    assert [(r.bill.number, r.balance) for r in rows] == [(a.number, D("100.00")), (b.number, D("50.00"))]
    assert [r.bill.number for r in bills_due(d(10, 31))] == [a.number]
    assert bills_due(d(10, 30)) == []


def test_vendor_balances(setup, vendor, zed, admin_user, expense):
    bill(vendor, admin_user, expense, "100.00", d(10, 1))
    bill(zed, admin_user, expense, "50.00", d(10, 5))
    assert [(r.vendor.name, r.balance) for r in vendor_balances()] == [("Acme Supply", D("100.00")), ("Zed Parts", D("50.00"))]
    assert [(r.vendor.name) for r in vendor_balances(d(10, 3))] == ["Acme Supply"]


def test_open_purchase_orders_list_what_is_left(setup, vendor, admin_user, make_item, inv_settings):
    item = make_item("WIDGET")
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(item, 100, D("1.00"))])
    bill_from_po(po, date=d(10, 5), vendor_invoice_number="V", quantities={po.lines.get().pk: 40}, user=admin_user)
    (row,) = open_purchase_orders()
    assert (row.order.number, row.ordered, row.billed, row.left) == (po.number, 100, 40, 60)
    bill_from_po(po, date=d(10, 6), vendor_invoice_number="W", quantities={po.lines.get().pk: 60}, user=admin_user)
    assert open_purchase_orders() == []


# ---- 1099 ----

@pytest.fixture
def v1099(db):
    return Vendor.objects.create(name="Contractor Co", is_1099=True, tax_id="12-3456789", address="1 Main St")


def pay(vendor, admin_user, cash, amount, on, method=M.ACH, paid_from=None, check="1"):
    return pay_vendor(vendor=vendor, date=on, method=method, amount=D(amount), paid_from=paid_from or cash,
                      user=admin_user, check_number=check if method == M.CHECK else "")


def test_1099_excludes_cards_non_flagged_and_voids(setup, v1099, vendor, admin_user, cash, card):
    pay(v1099, admin_user, cash, "1500.00", d(3, 1))
    pay(v1099, admin_user, cash, "400.00", d(4, 1), M.CHECK, check="10")
    pay(v1099, admin_user, card, "9000.00", d(5, 1), M.CARD)
    void_me = pay(v1099, admin_user, cash, "50.00", d(6, 1))
    void_vendor_payment(void_me, user=admin_user, reason="x")
    pay(vendor, admin_user, cash, "5000.00", d(7, 1))
    report = summary_1099(2026)
    assert [(r.vendor.name, r.total, r.file) for r in report.rows] == [("Contractor Co", D("1900.00"), False)]
    assert report.threshold == D("2000.00")


def test_1099_year_boundaries(setup, v1099, admin_user, cash):
    pay(v1099, admin_user, cash, "100.00", date(2025, 12, 31))
    pay(v1099, admin_user, cash, "200.00", date(2026, 1, 1))
    pay(v1099, admin_user, cash, "400.00", date(2026, 12, 31))
    pay(v1099, admin_user, cash, "800.00", date(2027, 1, 1))
    assert summary_1099(2025).rows[0].total == D("100.00")
    assert summary_1099(2026).rows[0].total == D("600.00")


def test_1099_threshold_edges_use_the_years_threshold(setup, v1099, admin_user, cash):
    pay(v1099, admin_user, cash, "1999.99", d(3, 1))
    assert summary_1099(2026).rows[0].file is False
    pay(v1099, admin_user, cash, "0.01", d(3, 2))
    assert summary_1099(2026).rows[0].file is True
    pay(v1099, admin_user, cash, "600.00", date(2025, 3, 1))
    assert summary_1099(2025).rows[0].file is True  # 2025 threshold is 600.00


def test_1099_without_a_threshold(setup, v1099, admin_user, cash):
    pay(v1099, admin_user, cash, "5000.00", date(2027, 3, 1))
    report = summary_1099(2027)
    assert report.threshold is None and report.rows[0].file is False


# ---- views ----

@pytest.fixture
def logged(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("path", [
    "/reports/ap-aging/", "/reports/bills-due/", "/reports/vendor-balances/", "/reports/open-purchase-orders/",
    "/reports/1099-summary/",
])
def test_reports_need_login_and_render(client, logged, path):
    assert logged.get(path).status_code == 200
    client.logout()
    assert client.get(path).status_code == 302


def test_ap_aging_page_and_csv(setup, logged, vendor, admin_user, expense):
    bill(vendor, admin_user, expense, "1234.50", d(10, 1))
    page = logged.get("/reports/ap-aging/?as_of=2026-10-31")
    assert b"1,234.50" in page.content and b"Acme Supply" in page.content
    r = logged.get("/reports/ap-aging/?as_of=2026-10-31&format=csv")
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0][0] == "Vendor" and rows[1][0] == "Acme Supply" and rows[1][-1] == "1234.50"
    assert rows[-2][0] == "A/P account" and rows[-1][0] == "Difference" and rows[-1][-1] == "0.00"
    assert 'filename="ap-aging-2026-10-31.csv"' in r["Content-Disposition"]


def test_bad_dates_show_a_message(logged):
    r = logged.get("/reports/ap-aging/?as_of=31/10/2026")
    assert r.status_code == 200 and b"Enter a date like 2026-01-31." in r.content
    r = logged.get("/reports/bills-due/?through=nope&format=csv")
    assert r.status_code == 200 and b"Enter a date like 2026-01-31." in r.content


def test_other_report_csvs(setup, logged, vendor, admin_user, expense, make_item, inv_settings):
    bill(vendor, admin_user, expense, "100.00", d(10, 1))
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(make_item("W"), 5, D("2.00"))])
    for name, needle in (
        ("bills-due", b"Acme Supply"), ("vendor-balances", b"100.00"), ("open-purchase-orders", po.number.encode())):
        r = logged.get(f"/reports/{name}/?format=csv" + ("&through=2026-12-31" if name == "bills-due" else ""))
        assert r["Content-Type"].startswith("text/csv") and needle in r.content, name


def test_1099_page_shows_note_masked_ids_and_threshold_message(setup, client, standard_user, v1099, admin_user, cash):
    pay(v1099, admin_user, cash, "2500.00", d(3, 1))
    client.force_login(standard_user)
    page = client.get("/reports/1099-summary/?year=2026")
    body = page.content.decode()
    assert "Check amounts and thresholds with your accountant before filing." in body
    assert "•••-••-6789" in body and "12-3456789" not in body
    assert "File" in body and "2,500.00" in body
    missing = client.get("/reports/1099-summary/?year=2031").content.decode()
    assert "Set the 1099 threshold for 2031 in Purchasing settings." in missing


def test_1099_csv_is_admin_only_and_has_full_tax_ids(setup, client, standard_user, admin_user, v1099, cash):
    pay(v1099, admin_user, cash, "2500.00", d(3, 1))
    client.force_login(standard_user)
    assert client.get("/reports/1099-summary/?year=2026&format=csv").status_code == 403
    client.force_login(admin_user)
    r = client.get("/reports/1099-summary/?year=2026&format=csv")
    rows = list(csv.reader(io.StringIO(r.content.decode())))
    assert rows[0] == ["vendor", "tax_id", "address", "total_paid", "file"]
    assert rows[1] == ["Contractor Co", "12-3456789", "1 Main St", "2500.00", "yes"]
    assert 'filename="1099-summary-2026.csv"' in r["Content-Disposition"]


def test_1099_bad_year(logged):
    r = logged.get("/reports/1099-summary/?year=abc")
    assert r.status_code == 200 and b"Enter a year like 2026." in r.content

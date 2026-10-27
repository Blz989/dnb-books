import html
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from contacts.models import Vendor
from items.stock import on_hand
from purchasing.balances import bill_balance, vendor_credit_unapplied, vendor_payment_unapplied
from purchasing.models import (
    BillPaymentApplication, VendorCredit, VendorCreditKind, VendorPayment, VendorPaymentMethod as M,
)
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill
from purchasing.posting.credits import AllowanceLineSpec, create_allowance_credit
from purchasing.posting.payments import PayLine, pay_vendor
from sales.models import DocStatus

D = Decimal


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings, expense):
    return purchasing_settings


@pytest.fixture
def zed(db):
    return Vendor.objects.create(name="Zed Parts")


def make_bill(vendor, admin_user, expense, amount, on=date(2026, 10, 1), ref=None):
    return create_bill(vendor=vendor, date=on, vendor_invoice_number=ref or f"R-{amount}", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D(amount))])


@pytest.fixture
def bills(setup, vendor, zed, admin_user, expense):
    return (make_bill(vendor, admin_user, expense, "100.00"), make_bill(vendor, admin_user, expense, "50.00"),
            make_bill(zed, admin_user, expense, "200.00"))


def text(response):
    return html.unescape(response.content.decode())


def pay_post(*pairs, method="ACH", paid_from=None, token="tok-pay-1", checks=None, **extra):
    data = {"token": token, "date": "2026-10-20", "method": method, "paid_from": paid_from.pk if paid_from else "",
            "vendor": "", "through": ""}
    for bill, amount in pairs:
        data[f"pay-{bill.pk}"] = amount
    for vendor, number in (checks or {}).items():
        data[f"check-{vendor.pk}"] = number
    data.update(extra)
    return data


def test_pages_need_login(client, setup):
    for path in ("/purchasing/pay/", "/purchasing/payments/", "/purchasing/payments/new/", "/purchasing/credits/",
                 "/purchasing/credits/new/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


# ---- pay bills ----

def test_pay_bills_lists_open_bills_and_filters(adm, bills, vendor, zed, cash):
    page = text(adm.get("/purchasing/pay/"))
    for bill in bills:
        assert bill.number in page
    assert "Pay in full" in page
    only = text(adm.get(f"/purchasing/pay/?vendor={zed.pk}"))
    assert bills[2].number in only and bills[0].number not in only
    due = text(adm.get("/purchasing/pay/?through=2026-10-15"))
    assert bills[0].number not in due  # NET_30 bills from Oct 1 are due Oct 31


def test_pay_bills_makes_one_payment_per_vendor_and_shows_them(adm, bills, vendor, zed, cash):
    a, b, z = bills
    r = adm.post("/purchasing/pay/", pay_post((a, "100.00"), (b, "50.00"), (z, "75.00"), method="CHECK", paid_from=cash,
                                              checks={vendor: "1001", zed: "1002"}))
    assert r.status_code == 302 and r["Location"].startswith("/purchasing/pay/done/")
    assert VendorPayment.objects.count() == 2
    page = text(adm.get(r["Location"]))
    assert "VP-00001" in page and "VP-00002" in page and "Acme Supply" in page and "Zed Parts" in page
    assert "150.00" in page and "75.00" in page and "1001" in page
    assert bill_balance(z) == D("125.00")


def test_pay_bills_partial_ach_and_card(adm, bills, cash, card):
    a = bills[0]
    adm.post("/purchasing/pay/", pay_post((a, "40.00"), method="ACH", paid_from=cash))
    adm.post("/purchasing/pay/", pay_post((a, "10.00"), method="CARD", paid_from=card, token="tok-card"))
    assert bill_balance(a) == D("50.00")
    assert sorted(p.method for p in VendorPayment.objects.all()) == ["ACH", "CARD"]


def test_duplicate_check_number_message_and_nothing_saved(adm, bills, vendor, zed, cash):
    a, _, z = bills
    r = adm.post("/purchasing/pay/", pay_post((a, "10.00"), (z, "10.00"), method="CHECK", paid_from=cash,
                                              checks={vendor: "1001", zed: "1001"}))
    assert r.status_code == 200 and "Check 1001 is already used on 1000 Cash." in text(r)
    assert VendorPayment.objects.count() == 0
    page = text(r)
    assert 'value="10.00"' in page  # what was typed is kept


def test_check_needs_a_number_and_card_needs_a_card(adm, bills, cash, vendor):
    a = bills[0]
    r = adm.post("/purchasing/pay/", pay_post((a, "10.00"), method="CHECK", paid_from=cash))
    assert "Enter the check number for Acme Supply." in text(r)
    r = adm.post("/purchasing/pay/", pay_post((a, "10.00"), method="CARD", paid_from=cash, token="t2"))
    assert "Choose the credit card used." in text(r)
    r = adm.post("/purchasing/pay/", pay_post((a, "10.00"), method="ACH", paid_from=None, token="t3"))
    assert "Choose the bank account to pay from." in text(r)
    assert VendorPayment.objects.count() == 0


def test_over_balance_and_bad_amount_and_nothing_chosen(adm, bills, cash):
    a = bills[0]
    r = adm.post("/purchasing/pay/", pay_post((a, "100.01"), paid_from=cash))
    assert f"{a.number} has 100.00 left to pay." in text(r)
    r = adm.post("/purchasing/pay/", pay_post((a, "abc"), paid_from=cash, token="t2"))
    assert f"{a.number}: enter an amount like 25.00." in text(r)
    r = adm.post("/purchasing/pay/", pay_post(paid_from=cash, token="t3"))
    assert "Choose at least one bill to pay." in text(r)


def test_payment_dated_before_the_bill(adm, bills, cash):
    r = adm.post("/purchasing/pay/", pay_post((bills[0], "10.00"), paid_from=cash, date="2026-09-01"))
    assert f"The date can't be before {bills[0].number}'s date." in text(r)


def test_pay_bills_double_submit(adm, bills, cash):
    data = pay_post((bills[0], "10.00"), paid_from=cash)
    adm.post("/purchasing/pay/", data)
    r = adm.post("/purchasing/pay/", data, follow=True)
    assert VendorPayment.objects.count() == 1
    assert "These payments were already saved." in text(r)


def test_standard_user_can_pay_bills(std, bills, cash):
    r = std.post("/purchasing/pay/", pay_post((bills[0], "10.00"), paid_from=cash))
    assert r.status_code == 302


# ---- single payment ----

def test_single_payment_keeps_the_extra_as_credit(adm, bills, vendor, cash):
    a, b, _ = bills
    start = adm.get(f"/purchasing/payments/new/?vendor={vendor.pk}")
    page = text(start)
    assert a.number in page and b.number in page
    r = adm.post("/purchasing/payments/new/", {
        "token": "tok-sp", "vendor": vendor.pk, "date": "2026-10-20", "method": "ACH", "paid_from": cash.pk,
        "check_number": "", "reference": "Deposit", "amount": "180.00", f"apply-{a.pk}": "100.00", f"apply-{b.pk}": "50.00"})
    payment = VendorPayment.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/purchasing/payments/{payment.number}/"
    assert vendor_payment_unapplied(payment) == D("30.00")
    detail = text(adm.get(r["Location"]))
    assert "30.00" in detail and a.number in detail and "Apply" in detail and "Deposit" in detail
    assert payment.number in text(adm.get("/purchasing/payments/"))


def test_single_payment_messages(adm, bills, vendor, cash):
    a = bills[0]
    base = {"token": "t", "vendor": vendor.pk, "date": "2026-10-20", "method": "ACH", "paid_from": cash.pk, "amount": "10.00"}
    r = adm.post("/purchasing/payments/new/", {**base, "amount": "0"})
    assert "Enter an amount above zero." in text(r)
    r = adm.post("/purchasing/payments/new/", {**base, "token": "t2", f"apply-{a.pk}": "20.00"})
    assert "This payment has 10.00 left to apply." in text(r)
    assert VendorPayment.objects.count() == 0


def test_apply_a_payment_later_and_void_rules(adm, std, bills, vendor, cash, admin_user):
    a = bills[0]
    payment = pay_vendor(vendor=vendor, date=date(2026, 10, 20), method=M.ACH, amount=D("30.00"), paid_from=cash, user=admin_user)
    page = text(adm.get(f"/purchasing/payments/{payment.number}/apply/"))
    assert a.number in page
    r = adm.post(f"/purchasing/payments/{payment.number}/apply/", {"token": "ap1", "date": "2026-10-21", f"apply-{a.pk}": "30.00"})
    assert r.status_code == 302 and vendor_payment_unapplied(payment) == D("0.00")
    assert std.get(f"/purchasing/payments/{payment.number}/void/").status_code == 403
    r = adm.post(f"/purchasing/payments/{payment.number}/void/", {"date": "2026-10-21", "reason": ""})
    assert "Enter a reason." in text(r)
    r = adm.post(f"/purchasing/payments/{payment.number}/void/", {"date": "2026-10-21", "reason": "Wrong vendor"})
    assert r.status_code == 302
    payment.refresh_from_db()
    assert payment.status == DocStatus.VOID and bill_balance(a) == D("100.00")


# ---- vendor credits ----

@pytest.fixture
def stock_bill(setup, vendor, admin_user, make_item):
    item = make_item("WIDGET", purchase_cost="0.4800")
    return item, create_bill(vendor=vendor, date=date(2026, 10, 5), vendor_invoice_number="V-1", user=admin_user,
                             item_lines=[BillItemSpec(item, 10, D("0.48"))])


def return_post(vendor, *rows, token="tok-ret", **extra):
    data = {"token": token, "vendor": vendor.pk, "date": "2026-10-10", "reason": "Wrong plating"}
    for i, (sku, qty, amount) in enumerate(rows):
        data.update({f"ret-{i}-sku": sku, f"ret-{i}-quantity": str(qty), f"ret-{i}-amount": amount})
    data.update(extra)
    return data


def test_return_credit_moves_stock_and_defaults_the_amount(adm, stock_bill, vendor):
    item, _ = stock_bill
    page = text(adm.get("/purchasing/credits/new/?kind=return"))
    assert "Return to vendor" in page
    r = adm.post("/purchasing/credits/new/?kind=return", return_post(vendor, ("WIDGET", 4, "")))
    credit = VendorCredit.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/purchasing/credits/{credit.number}/"
    assert on_hand(item) == 6 and credit.kind == VendorCreditKind.RETURN
    assert credit.lines.get().amount == D("1.92")
    detail = text(adm.get(r["Location"]))
    assert "1.92" in detail and "WIDGET" in detail and "Wrong plating" in detail


def test_return_credit_warns_before_negative_stock(adm, stock_bill, vendor):
    item, _ = stock_bill
    data = return_post(vendor, ("WIDGET", 12, "5.00"))
    page = text(adm.post("/purchasing/credits/new/?kind=return", data))
    assert "WIDGET has 10 on hand" in page and "Save anyway" in page
    assert VendorCredit.objects.count() == 0
    r = adm.post("/purchasing/credits/new/?kind=return", {**data, "allow_negative": "1"})
    assert r.status_code == 302 and on_hand(item) == -2


def test_return_credit_messages(adm, stock_bill, vendor):
    r = adm.post("/purchasing/credits/new/?kind=return", return_post(vendor))
    assert "Add at least one line." in text(r)
    r = adm.post("/purchasing/credits/new/?kind=return", return_post(vendor, ("NOPE", 1, ""), token="t2"))
    assert "Line 1: no item has SKU NOPE." in text(r)
    assert VendorCredit.objects.count() == 0


def allowance_post(vendor, account, amount="25.00", token="tok-al", **extra):
    data = {"token": token, "vendor": vendor.pk, "date": "2026-10-10", "reason": "Late",
            "lines-0-account": account.pk if account else "", "lines-0-amount": amount, "lines-0-description": "Late delivery"}
    data.update(extra)
    return data


def test_allowance_credit_and_apply(adm, bills, vendor, freight):
    r = adm.post("/purchasing/credits/new/?kind=allowance", allowance_post(vendor, freight))
    credit = VendorCredit.objects.get()
    assert r.status_code == 302 and credit.kind == VendorCreditKind.ALLOWANCE
    assert "Late delivery" in text(adm.get(r["Location"]))
    a = bills[0]
    page = text(adm.get(f"/purchasing/credits/{credit.number}/apply/"))
    assert a.number in page
    done = adm.post(f"/purchasing/credits/{credit.number}/apply/", {"token": "ap", "date": "2026-10-11", f"apply-{a.pk}": "25.00"})
    assert done.status_code == 302 and bill_balance(a) == D("75.00") and vendor_credit_unapplied(credit) == D("0.00")
    assert credit.number in text(adm.get("/purchasing/credits/"))


def test_allowance_messages(adm, bills, vendor, freight):
    r = adm.post("/purchasing/credits/new/?kind=allowance", allowance_post(vendor, None))
    assert "Line 1: choose an account." in text(r)
    r = adm.post("/purchasing/credits/new/?kind=allowance", allowance_post(vendor, freight, amount="x", token="t2"))
    assert "Line 1: enter an amount above zero." in text(r)
    assert VendorCredit.objects.count() == 0


def test_credit_apply_message_when_over(adm, bills, vendor, admin_user, freight):
    credit = create_allowance_credit(vendor=vendor, date=date(2026, 10, 10), user=admin_user, reason="",
                                     lines=[AllowanceLineSpec(freight, D("25.00"))])
    r = adm.post(f"/purchasing/credits/{credit.number}/apply/", {"token": "ap", "date": "2026-10-11", f"apply-{bills[0].pk}": "26.00"})
    assert "This credit has 25.00 left to apply." in text(r)
    r = adm.post(f"/purchasing/credits/{credit.number}/apply/", {"token": "ap2", "date": "2026-10-11"})
    assert "Enter an amount for at least one bill." in text(r)


def test_credit_void_is_admin_only_and_blocked_by_applications(adm, std, bills, vendor, admin_user, freight):
    credit = create_allowance_credit(vendor=vendor, date=date(2026, 10, 10), user=admin_user, reason="",
                                     lines=[AllowanceLineSpec(freight, D("25.00"))])
    assert std.get(f"/purchasing/credits/{credit.number}/void/").status_code == 403
    adm.post(f"/purchasing/credits/{credit.number}/apply/", {"token": "ap", "date": "2026-10-11", f"apply-{bills[0].pk}": "5.00"})
    r = adm.post(f"/purchasing/credits/{credit.number}/void/", {"date": "2026-10-11", "reason": "x"})
    assert "VC-00001 is applied to bills. Remove the applications first." in text(r)


def test_return_credit_void_restores_stock(adm, stock_bill, vendor):
    item, _ = stock_bill
    adm.post("/purchasing/credits/new/?kind=return", return_post(vendor, ("WIDGET", 4, "2.00")))
    credit = VendorCredit.objects.get()
    r = adm.post(f"/purchasing/credits/{credit.number}/void/", {"date": "2026-10-10", "reason": "Vendor refused"})
    assert r.status_code == 302 and on_hand(item) == 10


def test_standard_user_can_create_credits(std, stock_bill, vendor, freight):
    assert std.post("/purchasing/credits/new/?kind=return", return_post(vendor, ("WIDGET", 1, ""))).status_code == 302
    assert std.post("/purchasing/credits/new/?kind=allowance", allowance_post(vendor, freight, token="t9")).status_code == 302


def test_credit_double_submit(adm, stock_bill, vendor):
    data = return_post(vendor, ("WIDGET", 1, ""))
    adm.post("/purchasing/credits/new/?kind=return", data)
    adm.post("/purchasing/credits/new/?kind=return", data)
    assert VendorCredit.objects.count() == 1

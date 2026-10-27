"""Found in the final self-review of Phase 4: tax IDs through the Django admin, and bad input that must not give a 500."""
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from contacts.models import Vendor
from purchasing.posting.bills import BillExpenseSpec, create_bill

D = Decimal
HUGE = "9" * 40


@pytest.fixture
def staff_standard(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def staff_admin(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def tax_vendor(db):
    return Vendor.objects.create(name="Tax Co", is_1099=True, tax_id="12-3456789")


def test_standard_staff_cannot_read_a_full_tax_id_in_the_django_admin(staff_standard, tax_vendor):
    r = staff_standard.get(f"/admin/contacts/vendor/{tax_vendor.pk}/change/")
    body = r.content.decode()
    assert r.status_code == 200 and "12-3456789" not in body and "123456789" not in body and "•••-••-6789" in body


def test_admin_site_does_not_show_a_full_tax_id_either(staff_admin, tax_vendor):
    body = staff_admin.get(f"/admin/contacts/vendor/{tax_vendor.pk}/change/").content.decode()
    assert "12-3456789" not in body and "•••-••-6789" in body
    assert "12-3456789" not in staff_admin.get("/admin/contacts/vendor/").content.decode()


def test_saving_a_vendor_in_the_django_admin_keeps_its_tax_id(staff_admin, tax_vendor):
    staff_admin.post(f"/admin/contacts/vendor/{tax_vendor.pk}/change/", {
        "name": "Tax Co Inc", "contact_name": "", "email": "", "phone": "", "address": "", "terms": "NET_30",
        "is_1099": "on", "is_active": "on", "notes": ""})
    tax_vendor.refresh_from_db()
    assert tax_vendor.name == "Tax Co Inc" and tax_vendor.tax_id == "12-3456789"


@pytest.mark.parametrize("query", ["po=a/b", "po=NOPE", "po=%00", "po="])
def test_new_bill_with_a_bad_po_parameter_is_not_a_500(staff_admin, query):
    r = staff_admin.get(f"/purchasing/bills/new/?{query}")
    assert r.status_code in (200, 302, 404)


def rows(sku, qty, price):
    return {"lines-0-sku": sku, "lines-0-quantity": qty, "lines-0-price": price, "lines-0-description": "",
            "lines-0-ref": HUGE}


@pytest.fixture
def stuff(inv_settings, purchasing_settings, sales_settings, vendor, make_item, freight, cash):
    return make_item("WIDGET", purchase_cost="1.0000")


@pytest.mark.parametrize("qty, price", [(HUGE, "1.00"), ("5", HUGE), ("5", "1e400"), ("5", "nan"), ("5", "-inf"), ("1e9", "1")])
def test_absurd_numbers_in_a_po_or_bill_are_messages_not_500s(staff_admin, stuff, vendor, qty, price):
    po = staff_admin.post("/purchasing/orders/new/", {
        "token": "h1", "vendor": vendor.pk, "date": "2026-10-05", "expected_date": "", "notes": "", **rows("WIDGET", qty, price)})
    bill = staff_admin.post("/purchasing/bills/new/", {
        "token": "h2", "vendor": vendor.pk, "vendor_invoice_number": "X", "date": "2026-10-05", "terms": "", "notes": "",
        **rows("WIDGET", qty, price)})
    assert po.status_code == 200 and bill.status_code == 200
    assert b"banner error" in po.content and b"banner error" in bill.content


@pytest.mark.parametrize("amount", [HUGE, "1e400", "nan", "-5", "0.001"])
def test_absurd_expense_amounts_are_messages(staff_admin, stuff, vendor, freight, amount):
    r = staff_admin.post("/purchasing/bills/new/", {
        "token": "h3", "vendor": vendor.pk, "vendor_invoice_number": "X", "date": "2026-10-05", "terms": "", "notes": "",
        "exp-0-account": freight.pk, "exp-0-amount": amount, "exp-0-description": ""})
    assert r.status_code == 200 and b"banner error" in r.content


@pytest.mark.parametrize("amount", [HUGE, "1e400", "nan", "-5"])
def test_absurd_payment_and_credit_amounts_are_messages(staff_admin, stuff, vendor, freight, cash, admin_user, amount):
    bill = create_bill(vendor=vendor, date=date(2026, 10, 1), vendor_invoice_number="B", user=admin_user,
                       expense_lines=[BillExpenseSpec(freight, D("100.00"))])
    pay = staff_admin.post("/purchasing/pay/", {
        "token": "h4", "date": "2026-10-20", "method": "ACH", "paid_from": cash.pk, "vendor": "", "through": "",
        f"pay-{bill.pk}": amount})
    single = staff_admin.post("/purchasing/payments/new/", {
        "token": "h5", "vendor": vendor.pk, "date": "2026-10-20", "method": "ACH", "paid_from": cash.pk, "amount": amount})
    allowance = staff_admin.post("/purchasing/credits/new/?kind=allowance", {
        "token": "h6", "vendor": vendor.pk, "date": "2026-10-20", "lines-0-account": freight.pk, "lines-0-amount": amount})
    returned = staff_admin.post("/purchasing/credits/new/?kind=return", {
        "token": "h7", "vendor": vendor.pk, "date": "2026-10-20", "ret-0-sku": "WIDGET", "ret-0-quantity": "1",
        "ret-0-amount": amount})
    for r in (pay, single, allowance, returned):
        assert r.status_code == 200, r.status_code


@pytest.mark.parametrize("path", [
    "/purchasing/pay/?vendor=abc", "/purchasing/pay/?through=zzz", "/purchasing/pay/done/?n=%00,x",
    "/purchasing/payments/new/?vendor=abc", "/purchasing/credits/new/?kind=%00", "/purchasing/payments/?page=zzz",
    "/purchasing/bills/?vendor=a&status=%00&page=-1", "/purchasing/orders/?vendor=9999999999999999999999",
    "/vendors/?page=99999&q=%00", "/reports/1099-summary/?year=99999999999999999999",
    "/reports/bills-due/?through=9999-99-99", "/purchasing/bills/BILL-1/revisions/99999999999999999999/",
])
def test_odd_query_strings_do_not_give_a_500(staff_admin, stuff, path):
    assert staff_admin.get(path).status_code in (200, 302, 404)


def test_huge_ids_and_numbers_in_forms_and_urls_are_not_500s(staff_admin, stuff, vendor, admin_user, freight):
    bill = create_bill(vendor=vendor, date=date(2026, 10, 1), vendor_invoice_number="B", user=admin_user,
                       expense_lines=[BillExpenseSpec(freight, D("100.00"))])
    for path in (f"/vendors/{HUGE}/", f"/vendors/{HUGE}/edit/", "/purchasing/pay/?vendor=" + HUGE,
                 "/purchasing/payments/new/?vendor=" + HUGE, "/purchasing/bills/?vendor=" + HUGE,
                 "/purchasing/credits/?vendor=" + HUGE, "/purchasing/payments/?vendor=" + HUGE):
        assert staff_admin.get(path).status_code in (200, 404), path
    assert staff_admin.post(f"/purchasing/bills/{bill.number}/applications/payment/{HUGE}/remove/").status_code == 404
    for path, data in (
        ("/purchasing/orders/new/", {"vendor": HUGE, "date": "2026-10-05", **rows("WIDGET", "1", "1.00")}),
        ("/purchasing/bills/new/", {"vendor": HUGE, "date": "2026-10-05", **rows("WIDGET", "1", "1.00")}),
        ("/purchasing/payments/new/", {"vendor": HUGE, "date": "2026-10-05", "amount": "1"}),
        ("/purchasing/credits/new/?kind=allowance", {"vendor": HUGE, "date": "2026-10-05"}),
        ("/purchasing/bills/new/", {"vendor": vendor.pk, "date": "2026-10-05", "exp-0-account": HUGE, "exp-0-amount": "5"}),
        ("/purchasing/credits/new/?kind=allowance", {"vendor": vendor.pk, "date": "2026-10-05", "lines-0-account": HUGE,
                                                    "lines-0-amount": "5"}),
    ):
        r = staff_admin.post(path, {"token": f"x-{path}", **data})
        assert r.status_code == 200, (path, r.status_code)


def test_settings_numbers_and_thresholds_have_limits(staff_admin, db):
    r = staff_admin.post("/purchasing/settings/", {"action": "numbering", "prefix_BILL": "BILL-", "next_BILL": HUGE,
                                                   "prefix_PO": "PO-", "next_PO": "1", "prefix_VP": "VP-", "next_VP": "1",
                                                   "prefix_VC": "VC-", "next_VC": "1"})
    assert r.status_code == 200 and "Enter a smaller next number." in r.content.decode()
    r = staff_admin.post("/purchasing/settings/", {"action": "thresholds", "amount_2025": "600", "amount_2026": HUGE})
    assert r.status_code == 200 and "Enter an amount above zero." in r.content.decode()

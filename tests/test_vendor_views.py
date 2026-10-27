from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction

from contacts.models import Vendor
from purchasing.models import PurchasingSettings, Threshold1099
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import PayLine, pay_bills
from purchasing.models import VendorPaymentMethod as M
from sales.models import DocType
from sales.numbering import next_number

D = Decimal


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def std(client, standard_user):
    client.force_login(standard_user)
    return client


def vendor_post(**extra):
    data = {"name": "Midwest Fastener", "contact_name": "Pat", "email": "pat@mf.example", "phone": "555-0100",
            "address": "1 Main St", "terms": "NET_30", "default_expense_account": "", "tax_id": "", "notes": ""}
    data.update(extra)
    return data


def test_vendor_screens_need_login(client, vendor):
    for path in ("/vendors/", "/vendors/new/", f"/vendors/{vendor.pk}/", f"/vendors/{vendor.pk}/edit/", "/purchasing/settings/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


def test_list_shows_vendors_with_balances_and_hides_inactive(adm, vendor, admin_user, expense, purchasing_settings):
    create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="R", user=admin_user,
                expense_lines=[BillExpenseSpec(expense, D("1234.56"))])
    Vendor.objects.create(name="Gone Co", is_active=False)
    page = adm.get("/vendors/").content.decode()
    assert "Acme Supply" in page and "1,234.56" in page and "Overdue" in page and "Gone Co" not in page
    assert "Gone Co" in adm.get("/vendors/?inactive=1").content.decode()
    assert "Acme Supply" in adm.get("/vendors/?q=acme").content.decode()
    assert "No vendors match." in adm.get("/vendors/?q=zzz").content.decode()


def test_create_vendor(adm, expense):
    r = adm.post("/vendors/new/", vendor_post(default_expense_account=expense.pk, is_1099="on", tax_id="12-3456789"))
    v = Vendor.objects.get(name="Midwest Fastener")
    assert r.status_code == 302 and r["Location"] == f"/vendors/{v.pk}/"
    assert v.is_1099 and v.tax_id == "12-3456789" and v.default_expense_account == expense


def test_duplicate_and_bad_default_account_messages(adm, vendor, revenue):
    r = adm.post("/vendors/new/", vendor_post(name="ACME supply"))
    assert r.status_code == 200 and "A vendor named ACME supply already exists." in r.content.decode()
    r = adm.post("/vendors/new/", vendor_post(default_expense_account=revenue.pk))
    assert r.status_code == 200 and Vendor.objects.count() == 1
    assert "Choose an expense account." in r.content.decode() or "Select a valid choice" in r.content.decode()


def test_edit_vendor_and_empty_state(adm, vendor):
    r = adm.post(f"/vendors/{vendor.pk}/edit/", vendor_post(name="Acme Supply Inc", is_active="on"))
    assert r.status_code == 302
    vendor.refresh_from_db()
    assert vendor.name == "Acme Supply Inc"
    assert adm.get("/vendors/99999/").status_code == 404


def test_standard_user_can_add_and_edit(std, vendor):
    assert std.post("/vendors/new/", vendor_post()).status_code == 302
    assert std.post(f"/vendors/{vendor.pk}/edit/", vendor_post(name="Acme Renamed", is_active="on")).status_code == 302


def test_tax_id_is_masked_for_standard_users_everywhere(std, vendor):
    vendor.is_1099, vendor.tax_id = True, "12-3456789"
    vendor.save()
    for path in ("/vendors/", f"/vendors/{vendor.pk}/", f"/vendors/{vendor.pk}/edit/"):
        body = std.get(path).content.decode()
        assert "12-3456789" not in body and "123456789" not in body, path
    assert "•••-••-6789" in std.get(f"/vendors/{vendor.pk}/").content.decode()


def test_admin_sees_full_tax_id_only_on_the_edit_form(adm, vendor):
    vendor.is_1099, vendor.tax_id = True, "12-3456789"
    vendor.save()
    assert "12-3456789" in adm.get(f"/vendors/{vendor.pk}/edit/").content.decode()
    for path in ("/vendors/", f"/vendors/{vendor.pk}/"):
        body = adm.get(path).content.decode()
        assert "12-3456789" not in body and "•••-••-6789" in body, path


def test_standard_edit_keeps_a_masked_tax_id_and_can_replace_it(std, vendor):
    vendor.is_1099, vendor.tax_id = True, "12-3456789"
    vendor.save()
    masked = "•••-••-6789"
    std.post(f"/vendors/{vendor.pk}/edit/", vendor_post(name="Acme Supply", tax_id=masked, is_active="on", is_1099="on"))
    vendor.refresh_from_db()
    assert vendor.tax_id == "12-3456789"
    std.post(f"/vendors/{vendor.pk}/edit/", vendor_post(name="Acme Supply", tax_id="98-7654321", is_active="on", is_1099="on"))
    vendor.refresh_from_db()
    assert vendor.tax_id == "98-7654321"


@pytest.fixture
def owed(vendor, admin_user, expense, purchasing_settings):
    return create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="R", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("100.00"))])


def test_cannot_deactivate_with_a_balance(adm, vendor, owed):
    r = adm.post(f"/vendors/{vendor.pk}/deactivate/", follow=True)
    assert "Acme Supply has a balance of 100.00. Settle it before deactivating." in r.content.decode()
    vendor.refresh_from_db()
    assert vendor.is_active
    r = adm.post(f"/vendors/{vendor.pk}/edit/", vendor_post(name="Acme Supply"))  # is_active left off
    assert r.status_code == 200 and "Settle it before deactivating." in r.content.decode()


def test_deactivate_once_settled(adm, vendor, owed, admin_user, cash):
    pay_bills(date=date(2026, 1, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(owed, D("100.00"))])
    adm.post(f"/vendors/{vendor.pk}/deactivate/")
    vendor.refresh_from_db()
    assert not vendor.is_active


def test_detail_shows_open_bills_credits_and_history(adm, vendor, owed, admin_user, cash):
    from purchasing.posting.payments import pay_vendor

    pay_vendor(vendor=vendor, date=date(2026, 1, 20), method=M.ACH, amount=D("30.00"), paid_from=cash, user=admin_user)
    page = adm.get(f"/vendors/{vendor.pk}/").content.decode()
    assert owed.number in page and "VP-00001" in page and "/purchasing/payments/VP-00001/apply/" in page
    assert "70.00" in page  # balance 100 less 30 prepaid


# ---- settings ----

def test_settings_are_admin_only(std):
    assert std.get("/purchasing/settings/").status_code == 403
    assert std.post("/purchasing/settings/", {"action": "settings"}).status_code == 403


def test_settings_open_for_admins(adm):
    assert adm.get("/purchasing/settings/").status_code == 200


def test_settings_save_ap_account_and_only_offer_ap_accounts(adm, ap, cash, db):
    page = adm.get("/purchasing/settings/").content.decode()
    assert "2000 Accounts Payable" in page and "1000 Cash" not in page
    r = adm.post("/purchasing/settings/", {"action": "settings", "ap_account": ap.pk})
    assert r.status_code == 302 and PurchasingSettings.load().ap_account == ap
    r = adm.post("/purchasing/settings/", {"action": "settings", "ap_account": ""})
    assert r.status_code == 200 and "Choose an account." in r.content.decode()


def numbering_post(**extra):
    data = {"action": "numbering"}
    for t, prefix in (("PO", "PO-"), ("BILL", "BILL-"), ("VP", "VP-"), ("VC", "VC-")):
        data[f"prefix_{t}"], data[f"next_{t}"] = prefix, "1"
    data.update(extra)
    return data


def test_numbering_saved_and_checked(adm, db, vendor, admin_user, expense, purchasing_settings):
    r = adm.post("/purchasing/settings/", numbering_post(next_BILL="4567"))
    assert r.status_code == 302
    with transaction.atomic():
        assert next_number(DocType.BILL) == "BILL-04567"
    create_bill(vendor=vendor, date=date(2026, 1, 5), vendor_invoice_number="R", user=admin_user,
                expense_lines=[BillExpenseSpec(expense, D("5.00"))])
    r = adm.post("/purchasing/settings/", numbering_post(next_BILL="1"))
    assert r.status_code == 200 and "BILL-04568 is already used. Choose a higher next number." in r.content.decode()
    r = adm.post("/purchasing/settings/", numbering_post(next_VP="abc"))
    assert "Enter a next number of 1 or more." in r.content.decode()


def test_thresholds_add_and_edit(adm, db):
    page = adm.get("/purchasing/settings/").content.decode()
    assert "2025" in page and "2026" in page
    r = adm.post("/purchasing/settings/", {"action": "thresholds", "amount_2026": "2500.00", "amount_2025": "600.00",
                                           "new_year": "2027", "new_amount": "2500.00"})
    assert r.status_code == 302
    assert Threshold1099.objects.get(year=2026).amount == D("2500.00")
    assert Threshold1099.objects.get(year=2027).amount == D("2500.00")


def test_threshold_must_be_above_zero(adm, db):
    r = adm.post("/purchasing/settings/", {"action": "thresholds", "amount_2026": "0", "amount_2025": "600.00"})
    assert r.status_code == 200 and "Enter an amount above zero." in r.content.decode()
    assert Threshold1099.objects.get(year=2026).amount == D("2000.00")
    r = adm.post("/purchasing/settings/", {"action": "thresholds", "amount_2026": "2000.00", "amount_2025": "600.00",
                                           "new_year": "20x7", "new_amount": "100"})
    assert "Enter a year like 2026." in r.content.decode()


def test_sidebar_has_purchasing_group(adm):
    page = adm.get("/").content.decode()
    for text in ("Vendors", "Purchase orders", "Bills", "Pay bills", "Vendor credits", "Purchasing settings"):
        assert text in page, text


def test_sidebar_hides_purchasing_settings_from_standard_users(std):
    page = std.get("/").content.decode()
    assert "Vendors" in page and "Purchasing settings" not in page

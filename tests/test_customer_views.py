from datetime import date
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from contacts.models import Customer
from items.models import ItemType
from sales.models import SALES_DOC_TYPES, DocType, Invoice, PaymentMethod, SalesSettings
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import receive_payment

D = Decimal
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def std(standard_user):
    from django.test import Client

    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def form_data(**kw):
    data = {"name": "Birch Hardware", "contact_name": "Sam", "email": "sam@birch.example", "phone": "555-0111",
            "billing_address": "1 Birch Ln", "ship_to_address": "Dock 2", "terms": "NET_15", "resale_certificate": "",
            "notes": ""}
    data.update(kw)
    return data


def test_customer_pages_need_login(client, customer):
    for url in ("/customers/", "/customers/new/", f"/customers/{customer.pk}/", f"/customers/{customer.pk}/edit/"):
        assert client.get(url).status_code == 302


def test_create_customer_as_standard_user(std):
    r = std.post("/customers/new/", form_data())
    c = Customer.objects.get(name="Birch Hardware")
    assert r.status_code == 302 and r["Location"] == f"/customers/{c.pk}/" and c.terms == "NET_15"
    assert "Birch Hardware" in std.get("/customers/").content.decode()


def test_duplicate_name_message(adm, customer):
    r = adm.post("/customers/new/", form_data(name="ACME BUILDERS"))
    assert r.status_code == 200 and "A customer named ACME BUILDERS already exists." in r.content.decode()
    assert Customer.objects.count() == 1


def test_edit_customer(adm, customer):
    r = adm.post(f"/customers/{customer.pk}/edit/", {**form_data(name="Acme Builders"), "is_active": "on", "phone": "555-9999"})
    assert r.status_code == 302
    customer.refresh_from_db()
    assert customer.phone == "555-9999" and customer.terms == "NET_15"


def test_unknown_customer_is_404(adm):
    assert adm.get("/customers/9999/").status_code == 404


def test_list_shows_balance_and_overdue(adm, customer, admin_user, service):
    create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("120.00"))])
    page = adm.get("/customers/").content.decode()
    assert "120.00" in page and "Overdue" in page


def test_detail_shows_invoices_credits_and_history(adm, customer, admin_user, service):
    inv = create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("120.00"))])
    receive_payment(customer=customer, date=date(2026, 1, 10), method=PaymentMethod.CHECK, amount=D("150.00"),
                    user=admin_user, applications=[(inv, D("120.00"))])
    page = adm.get(f"/customers/{customer.pk}/").content.decode()
    assert "PMT-00001" in page and "30.00" in page and "Apply" in page and "Refund" in page
    assert "/sales/refunds/new/?payment=PMT-00001" in page


def test_standard_user_does_not_see_refund_action(std, customer, admin_user, service):
    receive_payment(customer=customer, date=date(2026, 1, 10), method=PaymentMethod.CASH, amount=D("30.00"), user=admin_user)
    page = std.get(f"/customers/{customer.pk}/").content.decode()
    assert "Apply" in page and "Refund" not in page


def test_deactivate_with_balance_is_refused(adm, customer, admin_user, service):
    create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("1200.00"))])
    r = adm.post(f"/customers/{customer.pk}/deactivate/", follow=True)
    assert "Acme Builders has a balance of 1,200.00. Settle it before deactivating." in r.content.decode()
    customer.refresh_from_db()
    assert customer.is_active


def test_deactivate_through_edit_form_with_balance_is_refused(adm, customer, admin_user, service):
    create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    r = adm.post(f"/customers/{customer.pk}/edit/", {**form_data(name="Acme Builders")})
    assert "Acme Builders has a balance of 5.00. Settle it before deactivating." in r.content.decode()


def test_deactivate_settled_customer(adm, customer):
    adm.post(f"/customers/{customer.pk}/deactivate/")
    customer.refresh_from_db()
    assert not customer.is_active
    assert f"/customers/{customer.pk}/" not in adm.get("/customers/").content.decode()
    assert f"/customers/{customer.pk}/" in adm.get("/customers/?inactive=1").content.decode()


# ---- sales settings ----

def test_settings_is_admin_only(std, adm):
    assert std.get("/sales/settings/").status_code == 403
    assert std.post("/sales/settings/", {"action": "settings"}).status_code == 403


def test_settings_page_for_admin(adm, sales_settings):
    assert adm.get("/sales/settings/").status_code == 200


def settings_post(sales_settings, **extra):
    data = {
        "action": "settings", "ar_account": sales_settings.ar_account_id,
        "undeposited_account": sales_settings.undeposited_account_id, "returns_account": sales_settings.returns_account_id,
        "company_name": "DNB Supply Co.", "company_address": "1 Main", "company_phone": "", "company_email": "",
        "default_invoice_notes": "Thanks",
    }
    data.update(extra)
    return data


def test_save_settings_and_logo(adm, sales_settings, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    r = adm.post("/sales/settings/", settings_post(sales_settings, logo=SimpleUploadedFile("l.png", PNG)))
    assert r.status_code == 302
    s = SalesSettings.load()
    assert s.company_name == "DNB Supply Co." and s.logo.name == "logo/company-logo.png"
    assert (tmp_path / "logo" / "company-logo.png").read_bytes() == PNG
    served = adm.get("/sales/settings/logo/")
    assert served.status_code == 200 and served["Content-Type"] == "image/png"


def test_logo_replaced_removes_old_file(adm, sales_settings, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    adm.post("/sales/settings/", settings_post(sales_settings, logo=SimpleUploadedFile("l.png", PNG)))
    adm.post("/sales/settings/", settings_post(sales_settings, logo=SimpleUploadedFile("l.jpg", JPEG)))
    assert SalesSettings.load().logo.name == "logo/company-logo.jpg"
    assert sorted(p.name for p in (tmp_path / "logo").iterdir()) == ["company-logo.jpg"]


@pytest.mark.parametrize("upload", [
    SimpleUploadedFile("evil.png", b"GIF89a" + b"\x00" * 20),
    SimpleUploadedFile("notes.png", b"just some text"),
    SimpleUploadedFile("big.png", PNG + b"\x00" * (1024 * 1024)),
])
def test_bad_logo_is_refused_and_old_logo_kept(adm, sales_settings, settings, tmp_path, upload):
    settings.MEDIA_ROOT = tmp_path
    adm.post("/sales/settings/", settings_post(sales_settings, logo=SimpleUploadedFile("l.png", PNG)))
    r = adm.post("/sales/settings/", settings_post(sales_settings, company_name="Changed", logo=upload))
    assert r.status_code == 200 and "Logo must be a PNG or JPEG under 1 MB." in r.content.decode()
    s = SalesSettings.load()
    assert s.logo.name == "logo/company-logo.png" and s.company_name == "DNB Supply Co."
    assert (tmp_path / "logo" / "company-logo.png").read_bytes() == PNG


def test_remove_logo(adm, sales_settings, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    adm.post("/sales/settings/", settings_post(sales_settings, logo=SimpleUploadedFile("l.png", PNG)))
    adm.post("/sales/settings/", settings_post(sales_settings, remove_logo="on"))
    assert not SalesSettings.load().logo
    assert adm.get("/sales/settings/logo/").status_code == 404


def test_settings_need_accounts(adm, sales_settings):
    r = adm.post("/sales/settings/", settings_post(sales_settings, ar_account=""))
    assert r.status_code == 200 and "Choose an account." in r.content.decode()


def numbering_post(**extra):
    data = {"action": "numbering"}
    for t in SALES_DOC_TYPES:
        data[f"prefix_{t.value}"] = {"SO": "SO-", "INV": "INV-", "PMT": "PMT-", "CN": "CN-", "DEP": "DEP-", "REF": "REF-"}[t.value]
        data[f"next_{t.value}"] = "1"
    data.update(extra)
    return data


def test_numbering_saved(adm, db):
    r = adm.post("/sales/settings/", numbering_post(next_INV="4567", prefix_INV="INV-"))
    assert r.status_code == 302
    from django.db import transaction

    from sales.numbering import next_number

    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-004567"


def test_numbering_conflict_message(adm, customer, admin_user, service):
    create_invoice(customer=customer, date=date(2026, 1, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    r = adm.post("/sales/settings/", numbering_post(next_INV="1"))
    assert r.status_code == 200 and "INV-000001 is already used. Choose a higher next number." in r.content.decode()


def test_numbering_rejects_non_numbers(adm, db):
    r = adm.post("/sales/settings/", numbering_post(next_INV="abc"))
    assert "Enter a next number of 1 or more." in r.content.decode()


def test_sidebar_has_sales_group(adm):
    page = adm.get("/").content.decode()
    for text in ("Customers", "Sales orders", "Invoices", "Payments", "Deposits", "Credit notes", "Sales settings"):
        assert text in page

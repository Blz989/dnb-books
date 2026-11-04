"""App screens that replaced Django admin (Books, Settings, Users), the reports split view, and the global logo."""
from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ledger.models import AccountingPeriod, EntryStatus, JournalEntry, JournalLine
from ledger.posting import LineSpec, post_entry
from sales.models import SalesSettings

D = Decimal
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def adm(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


# ---- shell ----

def test_sidebar_links_app_screens_not_django_admin(adm):
    page = adm.get("/").content.decode()
    for href in ("/books/journal/", "/books/accounts/", "/books/month-close/", "/settings/", "/reports/"):
        assert f'href="{href}"' in page, href
    assert "/admin/" not in page


def test_settings_link_is_admin_only(std):
    assert 'href="/settings/"' not in std.get("/").content.decode()
    for url in ("/settings/", "/settings/company/", "/settings/users/", "/settings/users/new/", "/settings/advanced/"):
        assert std.get(url).status_code == 403, url


def test_settings_index_opens_company_profile(adm):
    r = adm.get("/settings/")
    assert r.status_code == 302 and r["Location"] == "/settings/company/"


def test_module_settings_pages_share_the_settings_menu(adm, sales_settings):
    for url in ("/sales/settings/", "/purchasing/settings/", "/items/settings/", "/handling/settings/"):
        html = adm.get(url).content.decode()
        assert 'aria-label="Settings"' in html and 'href="/settings/company/"' in html, url


# ---- company profile and logo ----

def test_logo_shows_in_sidebar_and_login(adm, sales_settings, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    assert "/brand/logo/" not in adm.get("/").content.decode()
    adm.post("/settings/company/", {"company_name": "DNB Supply Co.", "logo": SimpleUploadedFile("l.png", PNG)})
    assert 'src="/brand/logo/"' in adm.get("/").content.decode()
    anon = Client()
    login = anon.get("/accounts/login/").content.decode()
    assert 'src="/brand/logo/"' in login and "DNB Supply Co." in login
    served = anon.get("/brand/logo/")  # public: the login page needs it
    assert served.status_code == 200 and served["Content-Type"] == "image/png"


def test_no_logo_returns_404(client, db):
    assert client.get("/brand/logo/").status_code == 404


def test_sales_settings_no_longer_takes_company_fields(adm, sales_settings):
    html = adm.get("/sales/settings/").content.decode()
    assert 'name="logo"' not in html and 'name="company_name"' not in html
    assert 'href="/settings/company/"' in html


def test_logo_on_handling_and_reconciliation_pdfs(sales_settings, settings, tmp_path):
    from core.company import logo_uri

    settings.MEDIA_ROOT = tmp_path
    assert logo_uri() is None
    (tmp_path / "logo").mkdir()
    (tmp_path / "logo" / "company-logo.png").write_bytes(PNG)
    s = SalesSettings.load()
    s.logo = "logo/company-logo.png"
    s.save()
    assert logo_uri() == (tmp_path / "logo" / "company-logo.png").as_uri()
    from pathlib import Path

    for template in ("handling/templates/handling/statement_pdf.html", "banking/templates/banking/reconcile_report_pdf.html"):
        assert "logo_uri" in Path(template).read_text(), template


# ---- users ----

def test_add_user_with_role(adm):
    r = adm.post("/settings/users/new/", {
        "username": "pat", "first_name": "Pat", "last_name": "Lee", "email": "", "is_active": "on",
        "role": "Admin", "password": "long-enough-pass-1"})
    assert r.status_code == 302
    pat = get_user_model().objects.get(username="pat")
    assert pat.check_password("long-enough-pass-1") and pat.groups.filter(name="Admin").exists()
    assert "Pat Lee" in adm.get("/settings/users/").content.decode()


def test_new_user_needs_password(adm):
    r = adm.post("/settings/users/new/", {"username": "pat", "role": "Standard", "is_active": "on"})
    assert r.status_code == 200 and "Enter a password." in r.content.decode()


def test_change_role_and_keep_password(adm, standard_user):
    r = adm.post(f"/settings/users/{standard_user.pk}/", {
        "username": "standard", "first_name": "", "last_name": "", "email": "", "is_active": "on",
        "role": "Admin", "password": ""})
    assert r.status_code == 302
    standard_user.refresh_from_db()
    assert standard_user.check_password("pw")
    assert list(standard_user.groups.values_list("name", flat=True)) == ["Admin"]


def test_admin_cannot_demote_or_block_self(adm, admin_user):
    adm.post(f"/settings/users/{admin_user.pk}/", {
        "username": "admin", "first_name": "", "last_name": "", "email": "", "role": "Standard", "password": ""})
    admin_user.refresh_from_db()
    assert admin_user.is_active and admin_user.groups.filter(name="Admin").exists()


# ---- journal entries ----

def test_journal_list_and_detail(adm, sample_ledger):
    html = adm.get("/books/journal/").content.decode()
    assert "JE-000001" in html and "Void" in html and "Draft" in html
    entry = JournalEntry.objects.get(pk=1)
    detail = adm.get(f"/books/journal/{entry.pk}/").content.decode()
    assert "10,000.00" in detail and "Manual entry" in detail


def test_journal_list_filters(adm, sample_ledger):
    html = adm.get("/books/journal/?status=DRAFT").content.decode()
    assert "JE-000001" not in html and "Draft" in html
    html = adm.get("/books/journal/?q=nothing-matches").content.decode()
    assert "No entries match these filters." in html


def test_journal_links_to_source_document(adm, admin_user, cash, revenue):
    entry = post_entry(date=date(2026, 3, 1), user=admin_user, source_type="sales.invoice", source_id="INV-000042",
                       lines=[LineSpec(cash, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    html = adm.get(f"/books/journal/{entry.pk}/").content.decode()
    assert 'href="/sales/invoices/INV-000042/"' in html


def test_new_journal_entry_posts(adm, cash, revenue):
    r = adm.post("/books/journal/new/", {
        "date": "2026-03-01", "reference": "ADJ-1", "memo": "Year-end",
        "account_1": cash.pk, "debit_1": "125.00", "account_2": revenue.pk, "credit_2": "125.00"})
    entry = JournalEntry.objects.get(reference="ADJ-1")
    assert r.status_code == 302 and r["Location"] == f"/books/journal/{entry.pk}/"
    assert entry.status == EntryStatus.POSTED and entry.lines.count() == 2


def test_new_journal_entry_unbalanced_shows_error_and_keeps_input(adm, cash, revenue):
    r = adm.post("/books/journal/new/", {
        "date": "2026-03-01", "account_1": cash.pk, "debit_1": "125.00", "account_2": revenue.pk, "credit_2": "100.00"})
    html = r.content.decode()
    assert r.status_code == 200 and "Debits (125.00) and credits (100.00) must be equal." in html
    assert 'value="125.00"' in html and not JournalEntry.objects.exists()


def test_new_journal_entry_line_without_account(adm, cash):
    r = adm.post("/books/journal/new/", {"date": "2026-03-01", "debit_1": "5", "account_2": cash.pk, "credit_2": "5"})
    assert "Line 1: choose an account." in r.content.decode()


def test_void_from_books(adm, posted_entry):
    r = adm.post(f"/books/journal/{posted_entry.pk}/void/", {"date": posted_entry.date.isoformat(), "reason": "Typo"})
    assert r.status_code == 302
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.VOID


def test_standard_user_cannot_void(std, posted_entry):
    assert std.get(f"/books/journal/{posted_entry.pk}/void/").status_code == 403
    assert "/void/" not in std.get(f"/books/journal/{posted_entry.pk}/").content.decode()


def test_post_and_delete_drafts(adm, admin_user, cash, revenue):
    def draft():
        e = JournalEntry.objects.create(date=date(2026, 2, 1), created_by=admin_user)
        JournalLine.objects.create(entry=e, account=cash, debit=D("10.00"))
        JournalLine.objects.create(entry=e, account=revenue, credit=D("10.00"))
        return e

    a, b = draft(), draft()
    adm.post(f"/books/journal/{a.pk}/post/")
    a.refresh_from_db()
    assert a.status == EntryStatus.POSTED
    adm.post(f"/books/journal/{b.pk}/delete/")
    assert not JournalEntry.objects.filter(pk=b.pk).exists()
    adm.post(f"/books/journal/{a.pk}/delete/")
    assert JournalEntry.objects.filter(pk=a.pk).exists()  # posted entries are never deleted


# ---- chart of accounts ----

def test_chart_of_accounts_shows_balances(adm, sample_ledger):
    html = adm.get("/books/accounts/").content.decode()
    assert "Assets" in html and "Income" in html and "10,700.00" in html  # cash: 10,000 + 1,000 - 300; the void nets to zero


def test_add_and_edit_account(adm, db):
    r = adm.post("/books/accounts/new/", {"number": "6200", "name": "Postage", "type": "EXPENSE",
                                         "subtype": "EXPENSE", "description": "", "is_active": "on"})
    assert r.status_code == 302
    from ledger.models import Account

    acct = Account.objects.get(number="6200")
    adm.post(f"/books/accounts/{acct.pk}/", {"number": "6200", "name": "Postage and shipping", "type": "EXPENSE",
                                             "subtype": "EXPENSE", "description": "", "is_active": "on"})
    acct.refresh_from_db()
    assert acct.name == "Postage and shipping"


def test_account_subtype_must_match_type(adm, db):
    r = adm.post("/books/accounts/new/", {"number": "6200", "name": "Bad", "type": "EXPENSE", "subtype": "BANK",
                                         "is_active": "on"})
    assert r.status_code == 200 and "That subtype doesn&#x27;t belong to this account type." in r.content.decode()


def test_account_type_locked_once_used(adm, posted_entry, cash):
    r = adm.post(f"/books/accounts/{cash.pk}/", {"number": cash.number, "name": cash.name, "type": "EXPENSE",
                                                 "subtype": cash.subtype, "is_active": "on"})
    cash.refresh_from_db()
    assert cash.type == "ASSET"
    assert r.status_code == 302


# ---- month close ----

def test_close_and_reopen_month(adm, posted_entry):
    month = f"{posted_entry.date:%Y-%m}"
    assert f"/books/month-close/{month}/close/" in adm.get("/books/month-close/").content.decode()
    r = adm.post(f"/books/month-close/{month}/close/")
    assert r.status_code == 302
    period = AccountingPeriod.objects.get(year=posted_entry.date.year, month=posted_entry.date.month)
    assert period.is_closed
    assert "Closed" in adm.get("/books/month-close/").content.decode()
    r = adm.post(f"/books/month-close/{month}/reopen/", {"reason": ""})
    assert "A reason is required to reopen a period." in r.content.decode()
    adm.post(f"/books/month-close/{month}/reopen/", {"reason": "Late bill"})
    period.refresh_from_db()
    assert not period.is_closed and period.reopen_reason == "Late bill"


def test_month_close_is_admin_only(std, posted_entry):
    month = f"{posted_entry.date:%Y-%m}"
    assert std.get("/books/month-close/").status_code == 200
    assert std.post(f"/books/month-close/{month}/close/").status_code == 403


def test_bad_month_is_404(adm, db):
    assert adm.post("/books/month-close/2026-13/close/").status_code == 404


# ---- reports ----

def test_reports_index_has_grouped_list_and_featured(adm, db):
    html = adm.get("/reports/").content.decode()
    for text in ("Business overview", "Receivables", "Payables", "Inventory", "Accountant", 'class="tile"'):
        assert text in html, text


def test_report_page_keeps_the_list_and_marks_current(adm, sample_ledger):
    html = adm.get("/reports/trial-balance/?as_of=2026-01-31").content.decode()
    assert 'aria-label="Reports"' in html and "11,500.00" in html
    assert '<a href="/reports/trial-balance/" class="on" aria-current="page">' in html
    assert 'href="/reports/ar-aging/"' in html

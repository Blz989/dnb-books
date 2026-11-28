from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from ledger.models import Account, AccountSubtype, AccountType, RecurringJournal
from ledger.recurring import Frequency, run_all
from purchasing.models import RecurringBill, RecurringBillLine

D = Decimal


@pytest.fixture
def rent_account(db):
    return Account.objects.create(number="6010", name="Rent", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)


@pytest.fixture
def setup(purchasing_settings, rent_account):
    return purchasing_settings


def bill_post(vendor, account, **extra):
    data = {"name": "Rent", "vendor": vendor.pk, "frequency": "MONTHLY", "start_date": "2026-12-01",
            "end_date": "", "terms": "", "notes": "Suite 4",
            "account_1": account.pk, "description_1": "Monthly rent", "amount_1": "2,400.00"}
    data.update(extra)
    return data


def test_create_recurring_bill(client, standard_user, vendor, setup, rent_account):
    client.force_login(standard_user)
    resp = client.post("/purchasing/recurring/new/", bill_post(vendor, rent_account))
    p = RecurringBill.objects.get()
    assert resp.status_code == 302 and resp["Location"] == f"/purchasing/recurring/{p.pk}/"
    assert (p.name, p.vendor, p.start_date, p.created_by, p.notes) == (
        "Rent", vendor, date(2026, 12, 1), standard_user, "Suite 4")
    line = p.lines.get()
    assert (line.account, line.amount, line.description) == (rent_account, D("2400.00"), "Monthly rent")


@pytest.mark.parametrize("extra,message", [
    ({"account_1": "", "description_1": "", "amount_1": ""}, "Add at least one line."),
    ({"account_1": ""}, "Line 1: choose an account."),
    ({"amount_1": "0"}, "Line 1: enter an amount above zero."),
    ({"end_date": "2026-11-01"}, "The end date can&#x27;t be before the start date."),
])
def test_recurring_bill_errors(client, standard_user, vendor, setup, rent_account, extra, message):
    client.force_login(standard_user)
    resp = client.post("/purchasing/recurring/new/", bill_post(vendor, rent_account, **extra))
    assert resp.status_code == 200
    assert message in resp.content.decode()
    assert not RecurringBill.objects.exists()


def test_recurring_bill_refuses_ap_and_inventory(client, standard_user, vendor, setup, ap, inventory_account):
    client.force_login(standard_user)
    html = client.post("/purchasing/recurring/new/", bill_post(vendor, ap)).content.decode()
    assert "Line 1: choose an account other than accounts payable." in html
    html = client.post("/purchasing/recurring/new/", bill_post(vendor, inventory_account)).content.decode()
    assert "Line 1: inventory changes through stock movements. Choose an expense account." in html


def make_profile(user, vendor, account, start=date(2026, 9, 1)):
    p = RecurringBill.objects.create(name="Rent", vendor=vendor, frequency=Frequency.MONTHLY, start_date=start,
                                     created_by=user)
    RecurringBillLine.objects.create(profile=p, account=account, amount=D("2400.00"))
    return p


def test_list_page_and_detail_with_created_bills(client, standard_user, vendor, setup, rent_account):
    p = make_profile(standard_user, vendor, rent_account)
    run_all(date(2026, 10, 15))
    client.force_login(standard_user)
    html = client.get("/purchasing/recurring/").content.decode()
    assert "Rent" in html and "2,400.00" in html and "Monthly" in html
    html = client.get(f"/purchasing/recurring/{p.pk}/").content.decode()
    assert html.count("BILL-") >= 2


def test_edit_changes_future_only(client, standard_user, vendor, setup, rent_account):
    p = make_profile(standard_user, vendor, rent_account)
    run_all(date(2026, 9, 15))
    client.force_login(standard_user)
    resp = client.post(f"/purchasing/recurring/{p.pk}/edit/",
                       bill_post(vendor, rent_account, start_date="2026-09-01", amount_1="2500.00"))
    assert resp.status_code == 302
    assert p.lines.get().amount == D("2500.00")
    from purchasing.balances import bill_total
    from purchasing.models import Bill
    assert bill_total(Bill.objects.get()) == D("2400.00")


def test_pause_resume_and_delete_rules(client, standard_user, vendor, setup, rent_account):
    p = make_profile(standard_user, vendor, rent_account, start=date(2030, 1, 1))
    client.force_login(standard_user)
    client.post(f"/purchasing/recurring/{p.pk}/pause/")
    p.refresh_from_db()
    assert not p.is_active
    client.post(f"/purchasing/recurring/{p.pk}/resume/")
    p.refresh_from_db()
    assert p.is_active and p.resume_from == timezone.localdate()
    resp = client.post(f"/purchasing/recurring/{p.pk}/delete/")
    assert resp.status_code == 302 and not RecurringBill.objects.exists()

    p = make_profile(standard_user, vendor, rent_account)
    run_all(date(2026, 9, 1))
    html = client.post(f"/purchasing/recurring/{p.pk}/delete/").content.decode()
    assert "This schedule has created documents. Set an end date instead of deleting it." in html
    assert RecurringBill.objects.filter(pk=p.pk).exists()


# ---- journal entries ----

def je_post(debit_account, credit_account, **extra):
    data = {"name": "Depreciation", "memo": "Truck", "frequency": "MONTHLY", "start_date": "2026-12-31",
            "end_date": "",
            "account_1": debit_account.pk, "debit_1": "250.00", "credit_1": "", "description_1": "",
            "account_2": credit_account.pk, "debit_2": "", "credit_2": "250.00", "description_2": ""}
    data.update(extra)
    return data


def test_create_recurring_journal(client, admin_user, expense, cash):
    client.force_login(admin_user)
    resp = client.post("/books/recurring/new/", je_post(expense, cash))
    p = RecurringJournal.objects.get()
    assert resp.status_code == 302
    assert [(l.account, l.debit, l.credit) for l in p.lines.all()] == [
        (expense, D("250.00"), D("0.00")), (cash, D("0.00"), D("250.00"))]


def test_recurring_journal_must_balance(client, admin_user, expense, cash):
    client.force_login(admin_user)
    html = client.post("/books/recurring/new/", je_post(expense, cash, credit_2="200.00")).content.decode()
    assert "Debits (250.00) and credits (200.00) must be equal. Difference: 50.00." in html
    assert not RecurringJournal.objects.exists()


def test_recurring_journal_list(client, admin_user):
    client.force_login(admin_user)
    assert "Recurring journal entries" in client.get("/books/recurring/").content.decode()


# ---- Home and cash forecast ----

def test_home_card_and_error_banner(client, admin_user, vendor, setup, rent_account):
    today = timezone.localdate()
    p = make_profile(admin_user, vendor, rent_account, start=today - timedelta(days=2))
    run_all(today)
    client.force_login(admin_user)
    html = client.get("/").content.decode()
    assert "Created by recurring schedules" in html and "BILL-" in html

    rent_account.is_active = False
    rent_account.save()
    make_profile(admin_user, vendor, rent_account, start=today)
    run_all(today)
    html = client.get("/").content.decode()
    assert "Recurring bill Rent couldn't be created for" in html


def test_cash_forecast_counts_recurring_bills(client, admin_user, vendor, setup, rent_account):
    from banking.forecast import build

    today = timezone.localdate()
    make_profile(admin_user, vendor, rent_account, start=today + timedelta(days=3))
    f = build(today, "monthly")
    flows = [x for x in f.flows if x.note.startswith("Recurring: Rent")]
    assert flows and flows[0].kind == "out" and flows[0].amount == D("2400.00")
    assert flows[0].expected == today + timedelta(days=33)  # billed in 3 days, Net 30
    client.force_login(admin_user)
    html = client.get("/reports/cash-forecast/?view=monthly").content.decode()
    assert "Recurring: Rent" in html

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from banking import reconcile
from banking.models import BankTransaction, PlaidItem, TxnSource, TxnStatus
from banking.register import ledger_balance
from banking.views.account_page import closing_balances
from ledger.posting import LineSpec, post_entry

D = Decimal


def post(user, day, bank, other, amount, memo):
    return post_entry(date=day, user=user, memo=memo, lines=[LineSpec(bank, debit=D(amount)),
                                                             LineSpec(other, credit=D(amount))])


def txn(account, ext, amount, status=TxnStatus.NEW, day=None, description="ROW"):
    return BankTransaction.objects.create(bank_account=account, external_id=ext, date=day or timezone.localdate(),
                                          description=description, amount=D(amount), source=TxnSource.FILE,
                                          status=status, exclude_reason="dup" if status == TxnStatus.EXCLUDED else "")


@pytest.fixture
def savings_account(db):
    from banking.models import AccountKind, BankAccount
    from ledger.models import Account, AccountSubtype, AccountType

    ledger = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    return BankAccount.objects.create(ledger_account=ledger, kind=AccountKind.SAVINGS)


@pytest.fixture
def page(admin_user, checking, bank, equity, revenue):
    today = timezone.localdate()
    post(admin_user, today - timedelta(days=40), bank, equity, "1000.00", "Owner money")
    post(admin_user, today - timedelta(days=3), bank, revenue, "250.00", "Kabayan Foods sale")
    return checking


def url(account, **params):
    return reverse("banking:account-detail", args=[account.pk]), params


def get(client, account, **params):
    path, query = url(account, **params)
    return client.get(path, query)


def test_tiles(client, admin_user, page, savings_account):
    client.force_login(admin_user)
    r = get(client, page)
    assert r.context["books"] == ledger_balance(page) == D("1250.00")
    html = r.content.decode()
    assert "Amount in books" in html and "1,250.00" in html and "Not reported" in html
    assert "File import" in html and "Import statement" in html and "None yet" in html
    page.bank_balance, page.bank_balance_on = D("1300.00"), date(2026, 10, 1)
    page.save()
    assert "as of 2026-10-01" in get(client, page).content.decode()


def test_bank_tile_falls_back_to_last_reconciliation(client, admin_user, page):
    today = timezone.localdate()
    rec = reconcile.start(page, statement_date=today - timedelta(days=30), statement_balance=D("1000.00"),
                          user=admin_user)
    for item in rec.items.all():
        reconcile.toggle(item, True)
    reconcile.finish(rec, user=admin_user)
    client.force_login(admin_user)
    r = get(client, page)
    assert r.context["bank"]["value"] == D("1000.00")
    assert f"statement, {rec.statement_date:%Y-%m-%d}" in r.content.decode()


def test_plaid_header_and_sync_for_admins_only(client, admin_user, standard_user, page, field_key):
    item = PlaidItem.objects.create(institution_name="First Platypus Bank")
    page.plaid_item = item
    page.save()
    client.force_login(admin_user)
    html = get(client, page).content.decode()
    assert "Not synced yet" in html and "Sync now" in html
    client.force_login(standard_user)
    html = get(client, page).content.decode()
    assert get(client, page).status_code == 200 and "Sync now" not in html


def test_sync_now_returns_to_the_page(client, admin_user, page, field_key, monkeypatch):
    from banking.views import accounts

    item = PlaidItem.objects.create(institution_name="First Platypus Bank")
    monkeypatch.setattr(accounts, "sync_item", lambda it: None)
    monkeypatch.setattr(accounts, "_report_sync", lambda *a: None)
    client.force_login(admin_user)
    back = reverse("banking:account-detail", args=[page.pk])
    r = client.post(reverse("banking:sync-now", args=[item.pk]), {"next": back})
    assert r["Location"] == back
    r = client.post(reverse("banking:sync-now", args=[item.pk]), {"next": "https://evil.example/"})
    assert r["Location"] == reverse("banking:accounts")


def test_dashboard(client, admin_user, page):
    txn(page, "a", "-5.00")
    txn(page, "b", "7.00", status=TxnStatus.BOOKED)
    client.force_login(admin_user)
    r = get(client, page)
    assert r.context["counts"]["new"] == 1 and r.context["counts"]["recognized"] == 1
    html = r.content.decode()
    for text in ("Last import", "Last reconciliation", "Transaction rules", "Total uncategorized", "Total recognized",
                 "Closing balance, last 30 days", "Recent transactions", "View more", "Uncategorized (1)"):
        assert text in html
    assert [row.line.entry.memo for row in r.context["recent"]] == ["Kabayan Foods sale", "Owner money"]
    assert r.context["recent"][0].balance == r.context["books"]


def test_closing_balances(page):
    today = timezone.localdate()
    points = closing_balances(page, today)
    assert len(points) == 30 and points[-1] == (today, D("1250.00"))
    assert points[0][1] == D("1000.00")


def test_uncategorized_tab_is_this_accounts_inbox(client, admin_user, page, savings_account):
    mine = txn(page, "a", "-5.00", description="MINE")
    txn(savings_account, "b", "-6.00", description="OTHER")
    hidden = txn(page, "c", "-7.00", status=TxnStatus.EXCLUDED, description="HIDDEN")
    client.force_login(admin_user)
    r = get(client, page, tab="uncategorized")
    assert [i["txn"] for i in r.context["items"]] == [mine]
    html = r.content.decode()
    assert "Show excluded" in html and "Confirm ticked rows" in html
    r = get(client, page, tab="uncategorized", status="EXCLUDED")
    assert [i["txn"] for i in r.context["items"]] == [hidden] and "Hide excluded" in r.content.decode()


def test_exclude_and_restore_from_the_tab_return_to_it(client, admin_user, page):
    row = txn(page, "a", "-5.00")
    client.force_login(admin_user)
    back = reverse("banking:account-detail", args=[page.pk]) + "?tab=uncategorized"
    r = client.post(reverse("banking:inbox-exclude", args=[row.pk]), {"reason": "personal", "token": "t1", "next": back})
    assert r["Location"] == back
    row.refresh_from_db()
    assert row.status == TxnStatus.EXCLUDED
    r = client.post(reverse("banking:inbox-restore", args=[row.pk]), {"next": back})
    assert r["Location"] == back
    row.refresh_from_db()
    assert row.status == TxnStatus.NEW


def test_change_page_returns_to_the_tab_and_rejects_other_sites(client, admin_user, page, expense):
    row = txn(page, "a", "-5.00")
    client.force_login(admin_user)
    back = reverse("banking:account-detail", args=[page.pk]) + "?tab=uncategorized"
    html = client.get(reverse("banking:inbox-change", args=[row.pk]), {"next": back}).content.decode()
    assert f'name="next" value="{back}"' in html.replace("&amp;", "&")
    r = client.post(reverse("banking:inbox-change", args=[row.pk]),
                    {"account": expense.pk, "token": "t2", "next": back})
    assert r["Location"] == back
    row2 = txn(page, "b", "-5.00")
    r = client.post(reverse("banking:inbox-exclude", args=[row2.pk]),
                    {"reason": "x", "token": "t3", "next": "https://evil.example/"})
    assert r["Location"] == reverse("banking:inbox")


def test_all_transactions_tab(client, admin_user, page):
    client.force_login(admin_user)
    r = get(client, page, tab="all")
    rows = r.context["rows"]
    assert rows[0].balance == r.context["books"] == D("1250.00")
    html = r.content.decode().replace("&amp;", "&")
    assert f"/reports/bank-register/?account={page.pk}&format=csv" in html
    r = get(client, page, tab="all", q="Kabayan")
    assert [row.line.entry.memo for row in r.context["rows"]] == ["Kabayan Foods sale"]
    assert f"account={page.pk}&q=Kabayan&format=pdf" in r.content.decode().replace("&amp;", "&")


def test_links_from_accounts_list_and_chart_of_accounts(client, admin_user, page):
    client.force_login(admin_user)
    target = reverse("banking:account-detail", args=[page.pk])
    assert target in client.get(reverse("banking:accounts")).content.decode()
    html = client.get(reverse("books:accounts")).content.decode()
    assert target in html and "Bank page" in html


@pytest.mark.parametrize("tab", ["dashboard", "uncategorized", "all"])
def test_query_count_is_flat(client, admin_user, page, bank, revenue, tab, django_assert_max_num_queries):
    client.force_login(admin_user)
    today = timezone.localdate()
    for n in range(3):
        post(admin_user, today - timedelta(days=n), bank, revenue, "1.00", f"s{n}")
        txn(page, f"x{n}", "-1.00")
    with django_assert_max_num_queries(200) as small:
        get(client, page, tab=tab)
    for n in range(3, 30):
        post(admin_user, today - timedelta(days=n % 25), bank, revenue, "1.00", f"s{n}")
    with django_assert_max_num_queries(len(small.captured_queries)):
        get(client, page, tab=tab)

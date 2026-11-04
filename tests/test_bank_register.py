import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from django.urls import reverse

from banking import reconcile
from banking.inbox import new_entry
from banking.models import BankTransaction, TxnSource
from banking.register import (
    FROM_BANK, MANUALLY_ADDED, MATCHED, RECONCILED, RegisterFilters, build_rows, filtered_lines, ledger_balance,
)
from core.report_catalog import catalog
from ledger.models import JournalEntry, JournalLine
from ledger.posting import LineSpec, post_entry, void_entry

D = Decimal


def d(month, day):
    return date(2026, month, day)


def post(user, day, bank, other, amount, memo, reference="", into=True):
    lines = [LineSpec(bank, debit=D(amount)), LineSpec(other, credit=D(amount))] if into else \
        [LineSpec(other, debit=D(amount)), LineSpec(bank, credit=D(amount))]
    return post_entry(date=day, user=user, memo=memo, reference=reference, lines=lines)


@pytest.fixture
def book(admin_user, checking, bank, revenue, expense, equity):
    entries = [
        post(admin_user, d(1, 2), bank, equity, "1000.00", "Owner money"),
        post(admin_user, d(1, 5), bank, expense, "120.50", "Office chairs", reference="2051", into=False),
        post(admin_user, d(1, 5), bank, revenue, "300.00", "Cash sale"),
        post(admin_user, d(1, 9), bank, expense, "45.25", "Paper", reference="2052", into=False),
        post(admin_user, d(2, 1), bank, revenue, "75.00", "Kabayan Foods sale"),
    ]
    return entries


def all_rows(account, f=None):
    return build_rows(account, list(filtered_lines(account, f or RegisterFilters())))


def expected_balances(account):
    """Running balance computed in Python from the ledger lines in date, entry, line order."""
    lines = JournalLine.objects.in_ledger().filter(account=account.ledger_account).order_by(
        "entry__date", "entry_id", "id")
    running, out = D("0.00"), {}
    for line in lines:
        running += line.debit - line.credit
        out[line.pk] = running
    return out


def test_running_balance_equals_ledger_balance_row_by_row(book, checking):
    rows = all_rows(checking)
    expected = expected_balances(checking)
    assert [r.balance for r in rows] == [expected[r.line.pk] for r in rows]
    assert [r.date for r in rows] == sorted((r.date for r in rows), reverse=True)
    assert rows[0].balance == ledger_balance(checking) == D("1209.25")
    assert rows[0].deposit == D("75.00") and rows[0].withdrawal is None


@pytest.mark.parametrize("f", [
    RegisterFilters(start=d(1, 5), end=d(1, 9)), RegisterFilters(q="paper"), RegisterFilters(q="300.00"),
    RegisterFilters(type="manual"), RegisterFilters(reconciled="no"),
])
def test_filters_never_change_a_row_balance(book, checking, f):
    expected = expected_balances(checking)
    rows = all_rows(checking, f)
    assert rows and all(r.balance == expected[r.line.pk] for r in rows)


def test_filters_narrow(book, checking):
    assert [r.line.entry.memo for r in all_rows(checking, RegisterFilters(start=d(1, 5), end=d(1, 9)))] == [
        "Paper", "Cash sale", "Office chairs"]
    assert [r.line.entry.memo for r in all_rows(checking, RegisterFilters(q="2052"))] == ["Paper"]
    assert [r.line.entry.memo for r in all_rows(checking, RegisterFilters(q="120.50"))] == ["Office chairs"]
    assert [r.reference for r in all_rows(checking, RegisterFilters(q="chairs"))] == ["2051"]
    assert all_rows(checking, RegisterFilters(type="sales.invoice")) == []
    assert len(all_rows(checking, RegisterFilters(type="manual"))) == 5


def test_void_and_reversal_both_show_and_net_to_zero(book, checking, admin_user):
    target = book[2]
    void_entry(target, user=admin_user, reason="wrong")
    rows = [r for r in all_rows(checking) if target.pk in (r.line.entry_id, r.line.entry.reverses_id)]
    assert len(rows) == 2
    assert sum((r.deposit or 0) - (r.withdrawal or 0) for r in rows) == 0
    top = all_rows(checking)[0]
    assert top.balance == ledger_balance(checking) == D("909.25")


def test_only_posted_lines(book, checking, bank, expense, admin_user):
    draft = JournalEntry.objects.create(date=d(3, 1), memo="Draft fee", created_by=admin_user)
    JournalLine.objects.create(entry=draft, account=bank, debit=D("5.00"))
    JournalLine.objects.create(entry=draft, account=expense, credit=D("5.00"))
    assert "Draft fee" not in [r.line.entry.memo for r in all_rows(checking)]


def test_statuses(book, checking, expense, admin_user):
    txn = BankTransaction.objects.create(bank_account=checking, external_id="x1", date=d(2, 3),
                                         description="BANK FEE", amount=D("-9.00"), source=TxnSource.FILE)
    new_entry(txn, account=expense, user=admin_user, memo="Bank fee")
    rec = reconcile.start(checking, statement_date=d(1, 31), statement_balance=D("1134.25"), user=admin_user)
    for item in rec.items.all():
        reconcile.toggle(item, True)
    reconcile.finish(rec, user=admin_user)
    by_memo = {r.line.entry.memo: r.status for r in all_rows(checking)}
    assert by_memo["Bank fee"] == FROM_BANK
    assert by_memo["Paper"] == RECONCILED
    assert by_memo["Kabayan Foods sale"] == MANUALLY_ADDED
    assert [r.line.entry.memo for r in all_rows(checking, RegisterFilters(reconciled="yes"))] == [
        "Paper", "Cash sale", "Office chairs", "Owner money"]


def test_matched_document(checking, admin_user, sales_settings, customer, bank):
    from banking.models import Match
    from sales.models import PaymentMethod
    from sales.posting.receipts import receive_payment

    payment = receive_payment(customer=customer, date=d(4, 1), method=PaymentMethod.ACH, amount=D("50.00"),
                              user=admin_user, bank_account=bank)
    txn = BankTransaction.objects.create(bank_account=checking, external_id="x2", date=d(4, 1),
                                         description="ACH ACME", amount=D("50.00"), source=TxnSource.FILE,
                                         status="MATCHED")
    Match.objects.create(bank_transaction=txn, sales_payment=payment, confirmed_by=admin_user)
    (row,) = [r for r in all_rows(checking) if r.line.entry_id == payment.journal_entry_id]
    assert row.status == MATCHED and row.counterparty == f"Customer: {customer.name}"
    assert row.type_label == "Customer payment" and row.url == reverse("sales:payment-detail", args=[payment.number])


def test_card_account_shows_what_is_owed(card_account, card, expense, admin_user):
    post_entry(date=d(1, 3), user=admin_user, memo="Gas", lines=[LineSpec(expense, debit=D("40.00")),
                                                                 LineSpec(card, credit=D("40.00"))])
    post_entry(date=d(1, 20), user=admin_user, memo="Card refund", lines=[LineSpec(card, debit=D("10.00")),
                                                                          LineSpec(expense, credit=D("10.00"))])
    rows = all_rows(card_account)
    assert [(r.deposit, r.withdrawal, r.balance) for r in rows] == [
        (None, D("10.00"), D("30.00")), (D("40.00"), None, D("40.00"))]
    assert ledger_balance(card_account) == D("30.00")


def test_report_query_count_is_flat(client, admin_user, checking, bank, revenue, django_assert_max_num_queries):
    client.force_login(admin_user)
    for n in range(10):
        post(admin_user, d(1, 1 + n), bank, revenue, "1.00", f"Sale {n}")
    url = reverse("reports:bank-register")
    with django_assert_max_num_queries(40) as small:
        client.get(url, {"account": checking.pk})
    for n in range(50):
        post(admin_user, d(2, 1 + n % 27), bank, revenue, "1.00", f"Sale b{n}")
    with django_assert_max_num_queries(len(small.captured_queries)):
        client.get(url, {"account": checking.pk})


def test_report_page_csv_pdf_and_catalog(client, admin_user, book, checking):
    client.force_login(admin_user)
    url = reverse("reports:bank-register")
    assert "Pick an account to see its register." in client.get(url).content.decode()
    page = client.get(url, {"account": checking.pk}).content.decode()
    assert "Bank register" in page and "Office chairs" in page and "1,209.25" in page
    response = client.get(url, {"account": checking.pk, "format": "csv"})
    assert response["Content-Disposition"] == f'attachment; filename="bank-register-{checking.ledger_account.number}.csv"'
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert rows[0] == ["Date", "Reference", "Type", "Counterparty", "Status", "Deposit", "Withdrawal", "Balance"]
    assert rows[1][0] == "2026-02-01" and rows[1][-1] == "1209.25" and len(rows) == 6
    assert client.get(url, {"account": checking.pk, "format": "pdf"})["Content-Type"] == "application/pdf"
    assert "bank-register" in [r["slug"] for g in catalog() for r in g["reports"]]

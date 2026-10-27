import html
import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking import reconcile
from banking.models import ReconStatus, Reconciliation
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.payments import pay_vendor, void_vendor_payment
from sales.models import PaymentMethod
from sales.posting.receipts import receive_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


def text(response):
    return html.unescape(response.content.decode())


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
def jan(sales_settings, purchasing_settings, admin_user, bank, customer, vendor, checking):
    pay = receive_payment(customer=customer, date=d(1, 3), method=PaymentMethod.ACH, amount=D("1000.00"),
                          user=admin_user, bank_account=bank)
    check = pay_vendor(vendor=vendor, date=d(1, 10), method=VM.CHECK, amount=D("300.00"), paid_from=bank,
                       user=admin_user, check_number="2050")
    return pay, check


def start(adm, checking, balance="700.00", on="2026-01-31"):
    r = adm.post(f"/banking/reconcile/{checking.pk}/", {"statement_date": on, "statement_balance": balance})
    return r


def item_ids(rec):
    return {i.journal_line.entry_id: i.pk for i in rec.items.select_related("journal_line")}


def test_pages_need_login(client, checking):
    for path in ("/banking/reconcile/", f"/banking/reconcile/{checking.pk}/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


def test_index_lists_accounts_with_last_reconciled(adm, jan, checking, card_account, admin_user):
    Reconciliation.objects.create(bank_account=checking, statement_date=d(1, 31), statement_balance=D("700.00"),
                                  status=ReconStatus.FINISHED, started_by=admin_user)
    page = text(adm.get("/banking/reconcile/"))
    assert "Cash" in page and "2026-01-31" in page and "700.00" in page and "Company Visa" in page
    assert "Never" in page


def test_start_form_then_working_page_shows_both_tables_and_the_difference(adm, jan, checking):
    assert "Statement ending balance" in text(adm.get(f"/banking/reconcile/{checking.pk}/"))
    r = start(adm, checking)
    rec = Reconciliation.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/banking/reconcile/{checking.pk}/{rec.pk}/"
    page = text(adm.get(r["Location"]))
    assert "Deposits and credits" in page and "Payments and debits" in page
    assert "1,000.00" in page and "300.00" in page
    assert "Statement balance" in page and "Difference" in page and "700.00" in page
    assert 'class="difference warn"' in page
    assert page.count('type="checkbox"') == 2


def test_start_validation_messages(adm, jan, checking):
    r = adm.post(f"/banking/reconcile/{checking.pk}/", {"statement_date": "", "statement_balance": "abc"})
    page = text(r)
    assert r.status_code == 200 and "Enter a date like 2026-01-31." in page and "Enter the balance" in page
    assert Reconciliation.objects.count() == 0


def test_visiting_start_with_one_in_progress_goes_to_it(adm, jan, checking):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    r = adm.get(f"/banking/reconcile/{checking.pk}/")
    assert r.status_code == 302 and r["Location"].endswith(f"/{rec.pk}/")
    again = start(adm, checking)
    assert again.status_code == 302 and Reconciliation.objects.count() == 1


def test_ticking_with_the_json_endpoint_updates_the_difference(adm, jan, checking):
    pay, check = jan
    start(adm, checking)
    rec = Reconciliation.objects.get()
    ids = item_ids(rec)
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{ids[pay.journal_entry_id]}/", {"cleared": "1"})
    assert r.json() == {"cleared": True, "cleared_total": "1,000.00", "cleared_balance": "1,000.00",
                        "difference": "-300.00", "can_finish": False}
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{ids[check.journal_entry_id]}/", {"cleared": "1"})
    assert r.json()["difference"] == "0.00" and r.json()["can_finish"] is True
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{ids[check.journal_entry_id]}/", {"cleared": "0"})
    assert r.json()["cleared"] is False and r.json()["difference"] == "-300.00"


def test_plain_form_ticks_work_without_javascript(adm, jan, checking):
    pay, check = jan
    start(adm, checking)
    rec = Reconciliation.objects.get()
    ids = item_ids(rec)
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/ticks/", {"cleared": [ids[pay.journal_entry_id]]}, follow=True)
    assert r.status_code == 200
    assert reconcile.difference(rec) == D("-300.00")
    adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/ticks/", {"cleared": list(ids.values())})
    assert reconcile.difference(rec) == D("0.00")


def test_the_other_accounts_items_cannot_be_ticked(adm, jan, checking, card_account):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    item = rec.items.first()
    assert adm.post(f"/banking/reconcile/{card_account.pk}/{rec.pk}/toggle/{item.pk}/", {"cleared": "1"}).status_code == 404


def test_finish_at_zero_then_the_finished_page_and_report(adm, jan, checking):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    for pk in item_ids(rec).values():
        adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{pk}/", {"cleared": "1"})
    page = text(adm.get(f"/banking/reconcile/{checking.pk}/{rec.pk}/"))
    assert 'class="difference ok"' in page and "Finish" in page
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/", follow=True)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED
    done = text(r)
    assert "Reconciled" in done and "report.pdf" in done and "Undo this reconciliation" in done
    pdf = adm.get(f"/banking/reconcile/{checking.pk}/{rec.pk}/report.pdf")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf"
    assert b"".join(pdf.streaming_content).startswith(b"%PDF")


def test_finish_refused_at_a_nonzero_difference(adm, jan, checking):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/", follow=True)
    assert "The difference must be 0.00 to finish." in text(r)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.IN_PROGRESS


def test_standard_user_can_tick_but_not_finish_or_undo(std, adm, jan, checking):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    for pk in item_ids(rec).values():
        assert std.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{pk}/", {"cleared": "1"}).status_code == 200
    page = text(std.get(f"/banking/reconcile/{checking.pk}/{rec.pk}/"))
    assert "Only an admin can finish" in page
    assert std.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/").status_code == 403
    adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/")
    assert std.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/undo/", {"reason": "x"}).status_code == 403


def test_undo_needs_a_reason_and_unlocks(adm, jan, checking, admin_user):
    pay, check = jan
    start(adm, checking)
    rec = Reconciliation.objects.get()
    for pk in item_ids(rec).values():
        adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/toggle/{pk}/", {"cleared": "1"})
    adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/finish/")
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/undo/", {"reason": " "}, follow=True)
    assert "Enter a reason." in text(r)
    rec.refresh_from_db()
    assert rec.status == ReconStatus.FINISHED
    adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/undo/", {"reason": "Statement was wrong"})
    rec.refresh_from_db()
    assert rec.status == ReconStatus.UNDONE
    void_vendor_payment(check, user=admin_user, reason="now allowed")


def test_cancel_removes_the_reconciliation(adm, jan, checking):
    start(adm, checking)
    rec = Reconciliation.objects.get()
    r = adm.post(f"/banking/reconcile/{checking.pk}/{rec.pk}/cancel/")
    assert r.status_code == 302 and not Reconciliation.objects.exists()


def test_card_screen_is_in_terms_of_the_amount_owed(adm, sales_settings, purchasing_settings, admin_user, card, card_account, vendor):
    pay_vendor(vendor=vendor, date=d(1, 4), method=VM.CARD, amount=D("250.00"), paid_from=card, user=admin_user)
    pay_vendor(vendor=vendor, date=d(1, 9), method=VM.CARD, amount=D("1000.00"), paid_from=card, user=admin_user)
    adm.post(f"/banking/reconcile/{card_account.pk}/", {"statement_date": "2026-01-31", "statement_balance": "1250.00"})
    rec = Reconciliation.objects.get()
    page = text(adm.get(f"/banking/reconcile/{card_account.pk}/{rec.pk}/"))
    assert "amount owed" in page and "Purchases and charges" in page and "Payments and credits" in page
    assert re.search(r'class="difference warn"[^>]*>[^<]*1,250\.00', page)


def test_unknown_reconciliation_is_a_404(adm, checking):
    assert adm.get(f"/banking/reconcile/{checking.pk}/9999/").status_code == 404

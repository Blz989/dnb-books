import html
import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking import reconcile
from banking.models import CheckLayout, CheckNumber, CheckStatus
from contacts.models import Vendor
from ledger.models import JournalEntry
from purchasing.models import VendorPayment, VendorPaymentMethod as VM
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import pay_bills, pay_vendor, PayLine
from sales.models import PaymentMethod
from sales.posting.receipts import issue_refund, receive_payment

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
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def acme(db):
    return Vendor.objects.create(name="Acme Supply Co.", address="PO Box 400\nDecatur, IL 62521")


@pytest.fixture
def to_print(setup, admin_user, acme, expense, bank, checking):
    bill = create_bill(vendor=acme, date=d(10, 27), vendor_invoice_number="88123", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("1075.00"))])
    return pay_vendor(vendor=acme, date=d(11, 3), method=VM.CHECK, amount=D("1075.00"), paid_from=bank,
                      user=admin_user, applications=[(bill, D("1075.00"))])


def token(page):
    return re.search(r'name="token" value="([^"]+)"', page).group(1)


def confirm(client, payment, number, result="yes", tok="tok-1"):
    return client.post(f"/banking/checks/{payment.number}/confirm/", {"number": number, "result": result, "token": tok})


# ---- list ----

def test_pages_need_login(client, to_print, checking):
    for path in ("/banking/checks/", f"/banking/checks/{to_print.number}/print/", f"/banking/layout/{checking.pk}/"):
        r = client.get(path)
        assert r.status_code == 302 and "/accounts/login/" in r["Location"], path


def test_list_shows_checks_to_print_oldest_first(adm, to_print, admin_user, bank, acme, customer, expense):
    older = pay_vendor(vendor=acme, date=d(10, 1), method=VM.CHECK, amount=D("40.00"), paid_from=bank, user=admin_user)
    pay_vendor(vendor=acme, date=d(10, 2), method=VM.CHECK, amount=D("41.00"), paid_from=bank, user=admin_user,
               check_number="3001")
    pay_vendor(vendor=acme, date=d(10, 3), method=VM.ACH, amount=D("42.00"), paid_from=bank, user=admin_user)
    pay = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    refund = issue_refund(source=pay, date=d(10, 6), bank_account=bank, method="CHECK", amount=D("10.00"),
                          user=admin_user)
    page = text(adm.get("/banking/checks/"))
    assert [page.index(n) for n in (older.number, refund.number, to_print.number)] == sorted(
        page.index(n) for n in (older.number, refund.number, to_print.number))
    assert "40.00" in page and "3001" not in page and "42.00" not in page
    assert "Acme Builders" in page  # the refund's customer


def test_empty_list(adm, setup):
    assert "No checks are waiting to be printed." in text(adm.get("/banking/checks/"))


def test_to_print_buttons_on_the_documents(adm, to_print, admin_user, bank, acme):
    assert "Print check" in text(adm.get(f"/purchasing/payments/{to_print.number}/"))
    numbered = pay_vendor(vendor=acme, date=d(11, 4), method=VM.CHECK, amount=D("5.00"), paid_from=bank,
                          user=admin_user, check_number="3001")
    assert "Print check" not in text(adm.get(f"/purchasing/payments/{numbered.number}/"))


def test_pay_bills_result_offers_printing(adm, setup, admin_user, acme, expense, bank, checking):
    bill = create_bill(vendor=acme, date=d(10, 27), vendor_invoice_number="1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("20.00"))])
    [p] = pay_bills(date=d(11, 3), method=VM.CHECK, paid_from=bank, user=admin_user, lines=[PayLine(bill, D("20.00"))])
    assert "Print check" in text(adm.get(f"/purchasing/pay/done/?n={p.number}"))


def test_refund_detail_offers_printing(adm, setup, admin_user, bank, customer, checking):
    pay = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    refund = issue_refund(source=pay, date=d(10, 6), bank_account=bank, method="CHECK", amount=D("10.00"),
                          user=admin_user)
    assert "Print check" in text(adm.get(f"/sales/refunds/{refund.number}/"))


# ---- print flow ----

def test_print_page_defaults_to_the_next_number_and_has_the_notice(adm, to_print, checking):
    page = text(adm.get(f"/banking/checks/{to_print.number}/print/"))
    assert 'value="2051"' in page
    assert "Print at 100% (actual size), not 'fit to page'." in page
    assert f"/banking/checks/{to_print.number}/pdf/?number=2051" in page and 'target="_blank"' in page
    assert "Did check 2051 print correctly?" in page and "No, it was spoiled" in page and ">Yes<" in page


def test_check_pdf_is_served_inline(adm, to_print, checking):
    r = adm.get(f"/banking/checks/{to_print.number}/pdf/?number=2051")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and "inline" in r["Content-Disposition"]
    assert r.content.startswith(b"%PDF")


def test_yes_records_printed_sets_the_number_and_advances_once(adm, to_print, checking):
    page = text(adm.get(f"/banking/checks/{to_print.number}/print/"))
    r = confirm(adm, to_print, "2051", tok=token(page))
    to_print.refresh_from_db()
    checking.refresh_from_db()
    assert r.status_code == 302
    assert to_print.check_number == "2051" and checking.next_check_number == 2052
    row = CheckNumber.objects.get()
    assert (row.number, row.status, row.vendor_payment, row.printed_by.username) == (
        "2051", CheckStatus.PRINTED, to_print, "admin")
    # double click: same token. Refresh and post again: a new token. Neither records anything more.
    confirm(adm, to_print, "2051", tok=token(page))
    confirm(adm, to_print, "2051", tok="another-token")
    checking.refresh_from_db()
    assert CheckNumber.objects.count() == 1 and checking.next_check_number == 2052


def test_second_attempt_after_printing_says_so(adm, to_print, checking):
    confirm(adm, to_print, "2051")
    r = adm.post(f"/banking/checks/{to_print.number}/confirm/", {"number": "2052", "result": "yes", "token": "t2"},
                 follow=True)
    assert "already printed as check 2051" in text(r)
    assert CheckNumber.objects.count() == 1


def test_spoiled_moves_to_the_next_number(adm, to_print, checking):
    r = confirm(adm, to_print, "2051", result="no")
    assert r.status_code == 302 and r["Location"].endswith(f"/banking/checks/{to_print.number}/print/")
    row = CheckNumber.objects.get()
    assert (row.number, row.status) == ("2051", CheckStatus.SPOILED)
    to_print.refresh_from_db()
    checking.refresh_from_db()
    assert to_print.check_number == "" and checking.next_check_number == 2052
    assert 'value="2052"' in text(adm.get(f"/banking/checks/{to_print.number}/print/"))


def test_a_used_number_entered_by_hand_is_refused_with_the_registry_message(adm, to_print, checking, admin_user, bank, acme):
    pay_vendor(vendor=acme, date=d(11, 4), method=VM.CHECK, amount=D("5.00"), paid_from=bank, user=admin_user,
               check_number="2060")
    page = text(adm.get(f"/banking/checks/{to_print.number}/print/?number=2060"))
    assert "Check 2060 was already used on 1000 Cash (handwritten)." in page
    assert "print correctly" not in page
    r = confirm(adm, to_print, "2060", tok="t9")
    assert CheckNumber.objects.filter(number="2060").count() == 1
    assert "was already used" in text(adm.get(f"/banking/checks/{to_print.number}/print/?number=2060"))
    to_print.refresh_from_db()
    assert to_print.check_number == "" and r.status_code in (200, 302)


def test_a_number_chosen_by_hand_is_used_for_the_pdf_and_the_prompt(adm, to_print, checking):
    page = text(adm.get(f"/banking/checks/{to_print.number}/print/?number=3100"))
    assert "number=3100" in page and "Did check 3100 print correctly?" in page


def test_a_blank_number_is_refused(adm, to_print, checking):
    r = adm.post(f"/banking/checks/{to_print.number}/confirm/", {"number": " ", "result": "yes", "token": "t"}, follow=True)
    assert "Enter a check number." in text(r)
    assert not CheckNumber.objects.exists()


def test_standard_user_can_print_the_first_time(std, to_print, checking):
    assert std.get(f"/banking/checks/{to_print.number}/print/").status_code == 200
    confirm(std, to_print, "2051")
    to_print.refresh_from_db()
    assert to_print.check_number == "2051"


def test_refund_check_prints_and_records(adm, setup, admin_user, bank, customer, checking):
    pay = receive_payment(customer=customer, date=d(10, 5), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    refund = issue_refund(source=pay, date=d(10, 6), bank_account=bank, method="CHECK", amount=D("10.00"),
                          user=admin_user)
    page = text(adm.get(f"/banking/checks/{refund.number}/print/"))
    assert "Acme Builders" in page and 'value="2051"' in page
    adm.post(f"/banking/checks/{refund.number}/confirm/", {"number": "2051", "result": "yes", "token": "r1"})
    refund.refresh_from_db()
    assert refund.reference == "2051"
    assert CheckNumber.objects.get().refund == refund
    assert adm.get(f"/banking/checks/{refund.number}/pdf/?number=2051").content.startswith(b"%PDF")


def test_numbered_voided_and_unknown_documents_cannot_be_printed(adm, to_print, checking):
    assert adm.get("/banking/checks/VP-99999/print/").status_code == 404
    confirm(adm, to_print, "2051")
    r = adm.get(f"/banking/checks/{to_print.number}/print/", follow=True)
    assert "already printed as check 2051" in text(r)


# ---- reprint ----

def test_reprint_spoils_the_old_number_and_is_admin_only(adm, std, to_print, checking):
    confirm(adm, to_print, "2051")
    assert std.post(f"/banking/checks/{to_print.number}/reprint/").status_code == 403
    r = adm.post(f"/banking/checks/{to_print.number}/reprint/")
    assert r.status_code == 302 and r["Location"].endswith(f"/banking/checks/{to_print.number}/print/")
    to_print.refresh_from_db()
    assert to_print.check_number == "" and CheckNumber.objects.get().status == CheckStatus.SPOILED
    assert 'value="2052"' in text(adm.get(r["Location"]))
    confirm(adm, to_print, "2052", tok="again")
    to_print.refresh_from_db()
    assert to_print.check_number == "2052"
    assert sorted(CheckNumber.objects.values_list("number", "status")) == [
        ("2051", CheckStatus.SPOILED), ("2052", CheckStatus.PRINTED)]


def test_reprint_of_a_reconciled_check_is_refused(adm, to_print, checking, admin_user):
    confirm(adm, to_print, "2051")
    rec = reconcile.start(checking, statement_date=d(11, 30), statement_balance=D("-1075.00"), user=admin_user)
    for item in rec.items.all():
        reconcile.toggle(item, True)
    reconcile.finish(rec, user=admin_user)
    r = adm.post(f"/banking/checks/{to_print.number}/reprint/", follow=True)
    assert "An admin must undo that reconciliation first." in text(r)
    to_print.refresh_from_db()
    assert to_print.check_number == "2051"


def test_reprint_when_nothing_was_printed(adm, to_print, checking):
    r = adm.post(f"/banking/checks/{to_print.number}/reprint/", follow=True)
    assert "has not been printed" in text(r)


# ---- layout ----

def test_layout_page_is_admin_only_and_shows_every_field(adm, std, checking):
    assert std.get(f"/banking/layout/{checking.pk}/").status_code == 403
    page = text(adm.get(f"/banking/layout/{checking.pk}/"))
    for label in ("Date", "Pay to", "Amount", "Amount in words", "Address block", "Memo", "Stub 1", "Stub 2"):
        assert label in page
    assert 'value="6.15"' in page and 'value="0.86"' in page
    assert "Print calibration page" in page and f"/banking/layout/{checking.pk}/calibrate.pdf" in page


def layout_post(checking, **over):
    from banking.checks import DEFAULT_LAYOUT

    data = {"offset_x": "0.00", "offset_y": "0.00"}
    for name, spec in DEFAULT_LAYOUT.items():
        data[f"x-{name}"], data[f"y-{name}"], data[f"size-{name}"] = str(spec["x"]), str(spec["y"]), str(spec["size"])
        data[f"visible-{name}"] = "on"
    data.update(over)
    return data


def test_layout_save_and_the_check_uses_it(adm, to_print, checking):
    r = adm.post(f"/banking/layout/{checking.pk}/", layout_post(
        checking, offset_x="0.10", offset_y="-0.05", **{"x-payee": "2.00"}))
    assert r.status_code == 302
    layout = CheckLayout.objects.get()
    assert (layout.offset_x, layout.offset_y) == (D("0.10"), D("-0.05")) and layout.fields["payee"]["x"] == 2.0
    page = text(adm.get(f"/banking/layout/{checking.pk}/"))
    assert 'value="0.10"' in page and 'value="2.0"' in page


def test_layout_offset_range_message(adm, checking):
    r = adm.post(f"/banking/layout/{checking.pk}/", layout_post(checking, offset_x="0.51"))
    assert r.status_code == 200 and "Offsets must be between -0.50 and 0.50 inches." in text(r)
    assert not CheckLayout.objects.exists()


def test_layout_visibility_checkbox(adm, checking):
    data = layout_post(checking)
    del data["visible-memo"]
    adm.post(f"/banking/layout/{checking.pk}/", data)
    assert CheckLayout.objects.get().fields["memo"]["visible"] is False


def test_calibration_pdf_is_served_to_admins_only(adm, std, checking):
    r = adm.get(f"/banking/layout/{checking.pk}/calibrate.pdf")
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf" and r.content.startswith(b"%PDF")
    assert std.get(f"/banking/layout/{checking.pk}/calibrate.pdf").status_code == 403


def test_two_tabs_confirming_at_once_print_one_check(adm, to_print, checking, monkeypatch):
    """Another tab records check 2051 between this request's first look and its write."""
    from banking.views import common

    real_claim = common._claim_token

    def other_tab_wins(token):
        VendorPayment.objects.filter(pk=to_print.pk).update(check_number="2051")
        CheckNumber.objects.create(bank_account=checking, number="2051", status=CheckStatus.PRINTED,
                                   vendor_payment=to_print)
        return real_claim(token)

    monkeypatch.setattr(common, "_claim_token", other_tab_wins)
    r = adm.post(f"/banking/checks/{to_print.number}/confirm/", {"number": "2052", "result": "yes", "token": "t2"},
                 follow=True)
    to_print.refresh_from_db()
    assert "already printed as check 2051" in text(r)
    assert to_print.check_number == "2051" and CheckNumber.objects.count() == 1

import re
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from banking import checks
from banking.checks import (
    DEFAULT_LAYOUT, LayoutError, calibration_pdf, check_context, check_pdf, layout_for, render_calibration_html,
    render_check_html, save_layout,
)
from banking.models import CheckLayout
from contacts.models import Vendor
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import pay_vendor
from sales.models import PaymentMethod
from sales.posting.receipts import issue_refund, receive_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def acme(db):
    return Vendor.objects.create(name="Acme Supply Co.", address="PO Box 400\nDecatur, IL 62521")


def make_bill(acme, admin_user, expense, amount, invoice, on):
    return create_bill(vendor=acme, date=on, vendor_invoice_number=invoice, user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D(amount))])


@pytest.fixture
def two_bill_payment(setup, acme, admin_user, expense, bank, checking):
    b1 = make_bill(acme, admin_user, expense, "1075.00", "88123", d(10, 27))
    b2 = make_bill(acme, admin_user, expense, "75.00", "88140", d(10, 30))
    return pay_vendor(vendor=acme, date=d(11, 3), method=VM.CHECK, amount=D("1075.00"), paid_from=bank,
                      user=admin_user, applications=[(b1, D("1000.00")), (b2, D("75.00"))])


def test_default_layout_matches_the_mockup_positions():
    assert {k: (v["x"], v["y"], v["size"]) for k, v in DEFAULT_LAYOUT.items()} == {
        "date": (6.15, 0.86, 10.5), "payee": (1.80, 1.29, 10.5), "amount": (6.78, 1.29, 10.5),
        "amount_words": (0.50, 1.71, 10.5), "address": (0.95, 2.10, 9.5), "memo": (0.95, 2.76, 9),
        "stub1": (0.45, 3.75, 9), "stub2": (0.45, 7.50, 9)}


def test_context_for_a_two_bill_payment_matches_the_mockup(two_bill_payment):
    c = check_context(two_bill_payment, "2051")
    assert c["date"] == "Nov 3, 2026"
    assert c["payee"] == "Acme Supply Co."
    assert c["amount"] == "**1,075.00"
    assert c["amount_words"] == "One thousand seventy-five and 00/100".ljust(70, "*")
    assert len(c["amount_words"]) == 70
    assert c["address_lines"] == ["Acme Supply Co.", "PO Box 400", "Decatur, IL 62521"]
    assert c["memo"] == "Inv 88123, 88140"
    stub = c["stub"]
    assert stub["summary"] == "Check 2051 · Nov 3, 2026 · $1,075.00"
    assert stub["columns"] == ["Bill", "Your invoice #", "Bill date", "Bill amount", "Paid"]
    assert stub["rows"] == [
        ["BILL-00001", "88123", "Oct 27, 2026", "1,075.00", "1,000.00"],
        ["BILL-00002", "88140", "Oct 30, 2026", "75.00", "75.00"]]
    assert stub["more"] == "" and stub["paid_from"] == "Paid from 1000 Cash"


def test_memo_is_cut_at_40_characters(setup, acme, admin_user, expense, bank):
    bills = [make_bill(acme, admin_user, expense, "1.00", f"INVOICE-{n:04d}", d(10, 1)) for n in range(8)]
    p = pay_vendor(vendor=acme, date=d(11, 3), method=VM.CHECK, amount=D("8.00"), paid_from=bank, user=admin_user,
                   applications=[(b, D("1.00")) for b in bills])
    memo = check_context(p, "1")["memo"]
    assert len(memo) == 40 and memo.startswith("Inv INVOICE-0000, INVOICE-0001")


def test_twelve_bills_all_show(setup, acme, admin_user, expense, bank):
    bills = [make_bill(acme, admin_user, expense, "1.00", f"I{n}", d(10, 1)) for n in range(12)]
    p = pay_vendor(vendor=acme, date=d(11, 3), method=VM.CHECK, amount=D("12.00"), paid_from=bank,
                   user=admin_user, applications=[(b, D("1.00")) for b in bills])
    stub = check_context(p, "1")["stub"]
    assert len(stub["rows"]) == 12 and stub["more"] == ""


def test_stub_overflow_at_13_bills(setup, acme, admin_user, expense, bank):
    bills = [make_bill(acme, admin_user, expense, "1.00", f"I{n}", d(10, 1)) for n in range(13)]
    p = pay_vendor(vendor=acme, date=d(11, 3), method=VM.CHECK, amount=D("13.00"), paid_from=bank,
                   user=admin_user, applications=[(b, D("1.00")) for b in bills])
    stub = check_context(p, "7")["stub"]
    assert len(stub["rows"]) == 11
    assert stub["more"] == "and 2 more (see statement)"


def test_refund_context_uses_the_customer_and_refund_number(setup, admin_user, bank, customer):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    refund = issue_refund(source=pay, date=d(1, 5), bank_account=bank, method="CHECK", amount=D("40.00"),
                          user=admin_user)
    c = check_context(refund, "2060")
    assert c["payee"] == "Acme Builders" and c["memo"] == refund.number
    assert c["address_lines"] == ["Acme Builders", "100 Main St", "Springfield, IL 62701"]
    assert c["stub"]["columns"] == ["Refund", "Source", "Amount"]
    assert c["stub"]["rows"] == [[refund.number, pay.number, "40.00"]]
    assert check_pdf(refund, "2060").startswith(b"%PDF")


def test_pdf_starts_with_pdf_marker(two_bill_payment):
    pdf = check_pdf(two_bill_payment, "2051")
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def lefts(html):
    return [Decimal(x) for x in re.findall(r"left:([0-9.]+)in", html)]


def tops(html):
    return [Decimal(x) for x in re.findall(r"top:([0-9.]+)in", html)]


def test_offset_shifts_every_field(two_bill_payment, checking, admin_user):
    base = render_check_html(two_bill_payment, "2051")
    assert lefts(base)[:3] == [D("6.15"), D("1.80"), D("6.78")]
    save_layout(checking, fields={}, offset_x=D("0.10"), offset_y=D("-0.05"), user=admin_user)
    shifted = render_check_html(two_bill_payment, "2051")
    assert len(lefts(shifted)) == len(lefts(base)) == 8
    assert [b + D("0.10") for b in lefts(base)] == lefts(shifted)
    assert [b - D("0.05") for b in tops(base)] == tops(shifted)


def test_saved_positions_override_defaults_and_hidden_fields_are_not_printed(
        two_bill_payment, checking, admin_user):
    save_layout(checking, fields={
        "payee": {"x": 2.0, "y": 1.3, "size": 11, "visible": True},
        "memo": {"x": 0.95, "y": 2.76, "size": 9, "visible": False}},
        offset_x=D("0"), offset_y=D("0"), user=admin_user)
    layout = layout_for(checking)
    assert layout["fields"]["payee"] == {"x": 2.0, "y": 1.3, "size": 11.0, "visible": True}
    assert layout["fields"]["date"] == DEFAULT_LAYOUT["date"]  # untouched fields keep the default
    html = render_check_html(two_bill_payment, "2051")
    assert "Inv 88123" not in html
    assert D("2.00") in lefts(html)


def test_offset_range_is_enforced(checking, admin_user):
    for bad in ("0.51", "-0.51"):
        with pytest.raises(LayoutError, match=r"Offsets must be between -0\.50 and 0\.50 inches\."):
            save_layout(checking, fields={}, offset_x=D(bad), offset_y=D("0"), user=admin_user)
        with pytest.raises(LayoutError, match=r"Offsets must be between -0\.50 and 0\.50 inches\."):
            save_layout(checking, fields={}, offset_x=D("0"), offset_y=D(bad), user=admin_user)
    save_layout(checking, fields={}, offset_x=D("0.50"), offset_y=D("-0.50"), user=admin_user)
    layout = CheckLayout.objects.get(bank_account=checking)
    assert (layout.offset_x, layout.offset_y) == (D("0.50"), D("-0.50"))


def test_layout_validation_messages(checking, admin_user):
    with pytest.raises(LayoutError, match="Pay to: the font size must be between 6 and 20."):
        save_layout(checking, fields={"payee": {"x": 1, "y": 1, "size": 40}}, offset_x=D("0"), offset_y=D("0"),
                    user=admin_user)
    with pytest.raises(LayoutError, match="Date: the position must be on the page."):
        save_layout(checking, fields={"date": {"x": 9, "y": 1, "size": 10}}, offset_x=D("0"), offset_y=D("0"),
                    user=admin_user)
    with pytest.raises(LayoutError, match="Memo: enter a position and a font size."):
        save_layout(checking, fields={"memo": {"x": "", "y": 1, "size": 10}}, offset_x=D("0"), offset_y=D("0"),
                    user=admin_user)


def test_save_layout_is_admin_only(checking, standard_user):
    with pytest.raises(PermissionDenied):
        save_layout(checking, fields={}, offset_x=D("0"), offset_y=D("0"), user=standard_user)


def test_calibration_page_shows_boxes_grid_labels_and_offsets(checking, admin_user):
    save_layout(checking, fields={}, offset_x=D("0.10"), offset_y=D("-0.20"), user=admin_user)
    html = render_calibration_html(checking)
    assert "Offset X +0.10 in" in html and "Offset Y -0.20 in" in html
    assert html.count('class="box"') == 8
    assert html.count("vline") == 35 and html.count("hline") == 45  # every 0.25 in across 8.5 x 11 in
    assert 'Pay to' in html and "Print at 100% (actual size)" in html
    assert calibration_pdf(checking).startswith(b"%PDF")

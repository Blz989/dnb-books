from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction

from contacts.models import Terms
from purchasing import balances as pb
from purchasing.errors import PurchasingSettingsMissing
from purchasing.models import (
    Bill, BillItemLine, BillLineKind, BillPaymentApplication, PurchasingSettings, Threshold1099, VendorCredit,
    VendorCreditApplication, VendorCreditKind, VendorCreditLine, VendorPayment, VendorPaymentMethod,
)
from sales.errors import SalesError
from sales.models import DocStatus, DocType
from sales.numbering import next_number, set_sequence

D = Decimal


def test_bill_numbers_use_the_bill_sequence(db):
    with transaction.atomic():
        assert next_number(DocType.BILL) == "BILL-00001"
        assert next_number(DocType.PO) == "PO-00001"
        assert next_number(DocType.BILL) == "BILL-00002"


def test_set_sequence_works_for_vendor_payments(db, admin_user):
    set_sequence(DocType.VP, prefix="VP-", next_number=120, user=admin_user)
    with transaction.atomic():
        assert next_number(DocType.VP) == "VP-00120"


def test_thresholds_are_seeded(db):
    assert Threshold1099.objects.get(year=2025).amount == D("600.00")
    assert Threshold1099.objects.get(year=2026).amount == D("2000.00")


def test_settings_must_be_configured(db, ap):
    s = PurchasingSettings.load()
    assert not s.is_configured
    with pytest.raises(PurchasingSettingsMissing, match="Set up purchasing accounts first."):
        s.require_configured()
    s.ap_account = ap
    s.save()
    assert PurchasingSettings.load().require_configured().ap_account == ap


def _bill(vendor, admin_user, number, on=date(2026, 3, 1), amount="100.00", item=None):
    bill = Bill.objects.create(
        number=number, vendor=vendor, vendor_invoice_number=number, date=on, terms=Terms.NET_30,
        due_date=date(2026, 3, 31), created_by=admin_user)
    BillItemLine.objects.create(
        bill=bill, item=item, quantity=1, price=D(amount), amount=D(amount), kind=BillLineKind.NON_STOCK)
    return bill


@pytest.fixture
def bill(vendor, admin_user, make_item, inv_settings):
    return _bill(vendor, admin_user, "BILL-00001", item=make_item("W1", purchase_cost="1.00"))


def _payment(vendor, admin_user, cash, amount="60.00", on=date(2026, 3, 5)):
    return VendorPayment.objects.create(
        number="VP-00001", vendor=vendor, date=on, method=VendorPaymentMethod.ACH, paid_from=cash,
        amount=D(amount), created_by=admin_user)


def test_bill_balance_follows_applications_as_of_a_date(bill, vendor, admin_user, cash):
    payment = _payment(vendor, admin_user, cash)
    app = BillPaymentApplication.objects.create(payment=payment, bill=bill, amount=D("60.00"), applied_on=date(2026, 3, 5))
    assert pb.bill_total(bill) == D("100.00")
    assert pb.bill_balance(bill) == D("40.00")
    assert pb.bill_balance(bill, date(2026, 3, 4)) == D("100.00")
    assert pb.bill_balance(bill, date(2026, 3, 5)) == D("40.00")
    app.removed, app.removed_on = True, date(2026, 3, 10)
    app.save()
    assert pb.bill_balance(bill) == D("100.00")
    assert pb.bill_balance(bill, date(2026, 3, 9)) == D("40.00")
    assert pb.bill_balance(bill, date(2026, 3, 10)) == D("100.00")


def test_void_bill_has_no_balance_from_its_void_date(bill):
    bill.status, bill.voided_on = DocStatus.VOID, date(2026, 3, 20)
    bill.save()
    assert pb.bill_balance(bill) == D("0.00")
    assert pb.bill_balance(bill, date(2026, 3, 19)) == D("100.00")
    assert pb.bill_balance(bill, date(2026, 2, 28)) == D("0.00")


def test_payment_unapplied_and_vendor_balance(bill, vendor, admin_user, cash):
    payment = _payment(vendor, admin_user, cash, amount="130.00")
    BillPaymentApplication.objects.create(payment=payment, bill=bill, amount=D("100.00"), applied_on=date(2026, 3, 5))
    assert pb.vendor_payment_unapplied(payment) == D("30.00")
    assert pb.vendor_balance(vendor) == D("-30.00")


def test_credit_unapplied_and_credit_application(bill, vendor, admin_user, expense):
    credit = VendorCredit.objects.create(
        number="VC-00001", vendor=vendor, date=date(2026, 3, 6), kind=VendorCreditKind.ALLOWANCE, created_by=admin_user)
    VendorCreditLine.objects.create(credit=credit, account=expense, amount=D("25.00"))
    assert pb.credit_total(credit) == D("25.00")
    assert pb.vendor_credit_unapplied(credit) == D("25.00")
    VendorCreditApplication.objects.create(credit=credit, bill=bill, amount=D("10.00"), applied_on=date(2026, 3, 7))
    assert pb.vendor_credit_unapplied(credit) == D("15.00")
    assert pb.bill_balance(bill) == D("90.00")
    assert pb.vendor_balance(vendor) == D("75.00")
    assert pb.vendor_credit_unapplied(credit, date(2026, 3, 6)) == D("25.00")


def test_invoice_changed_hook_calls_registered_functions(db, monkeypatch):
    from sales import hooks

    seen = []
    monkeypatch.setattr(hooks, "_invoice_changed", [])
    hooks.register_invoice_changed(seen.append)
    hooks.register_invoice_changed(seen.append)  # registering twice calls once
    hooks.invoice_changed("INV")
    assert seen == ["INV"]


def test_sales_number_tests_still_see_sales_errors(db, admin_user):
    with pytest.raises(SalesError):
        set_sequence(DocType.BILL, prefix="BILL-", next_number=0, user=admin_user)

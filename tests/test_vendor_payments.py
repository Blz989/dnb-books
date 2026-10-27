from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from contacts.models import Vendor
from ledger.balances import account_totals, money
from ledger.models import Account, AccountSubtype, AccountType, JournalEntry
from purchasing.balances import bill_balance, vendor_balance, vendor_payment_unapplied
from purchasing.errors import PurchasingError
from purchasing.models import BillPaymentApplication, VendorPayment, VendorPaymentMethod as M
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import (
    PayLine, apply_vendor_payment, pay_bills, pay_vendor, remove_bill_application, void_vendor_payment,
)
from sales.models import DocStatus

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


def ap_balance(ap):
    debit, credit = account_totals(start=None, end=date.max).get(ap.pk, (D("0.00"), D("0.00")))
    return money(credit - debit)


@pytest.fixture
def setup(purchasing_settings, expense):
    return purchasing_settings


@pytest.fixture
def bank2(db):
    return Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)


@pytest.fixture
def zed(db):
    return Vendor.objects.create(name="Zed Parts")


def bill(vendor, admin_user, expense, amount, on=d(10, 5), ref="V"):
    return create_bill(vendor=vendor, date=on, vendor_invoice_number=f"{ref}-{amount}", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D(amount))])


@pytest.fixture
def three_bills(setup, vendor, zed, admin_user, expense):
    return (bill(vendor, admin_user, expense, "100.00"), bill(vendor, admin_user, expense, "50.00"),
            bill(zed, admin_user, expense, "200.00"))


def test_batch_makes_one_payment_per_vendor(three_bills, admin_user, cash, ap):
    a, b, z = three_bills
    payments = pay_bills(
        date=d(10, 20), method=M.CHECK, paid_from=cash, user=admin_user,
        lines=[PayLine(z, D("200.00")), PayLine(a, D("100.00")), PayLine(b, D("50.00"))],
        check_numbers={a.vendor_id: "1001", z.vendor_id: "1002"})
    assert [(p.vendor.name, p.amount, p.check_number) for p in payments] == [
        ("Acme Supply", D("150.00"), "1001"), ("Zed Parts", D("200.00"), "1002")]
    assert [p.number for p in payments] == ["VP-00001", "VP-00002"]
    assert entry_lines(payments[0].journal_entry) == [("1000", D("0"), D("150.00")), ("2000", D("150.00"), D("0"))]
    assert (payments[0].journal_entry.source_type, payments[0].journal_entry.source_id) == ("purchasing.payment", "VP-00001")
    assert sorted((x.bill.number, x.amount) for x in payments[0].applications.all()) == [
        (a.number, D("100.00")), (b.number, D("50.00"))]
    assert [bill_balance(x) for x in (a, b, z)] == [D("0.00")] * 3
    assert ap_balance(ap) == D("0.00")


def test_partial_payment(three_bills, admin_user, bank2, ap):
    a = three_bills[0]
    (payment,) = pay_bills(date=d(10, 20), method=M.ACH, paid_from=bank2, user=admin_user, lines=[PayLine(a, D("40.00"))])
    assert payment.check_number == "" and payment.method == M.ACH
    assert bill_balance(a) == D("60.00")
    assert ap_balance(ap) == D("310.00")


def test_card_payment_credits_the_card_account(three_bills, admin_user, card):
    a = three_bills[0]
    (payment,) = pay_bills(date=d(10, 20), method=M.CARD, paid_from=card, user=admin_user, lines=[PayLine(a, D("100.00"))])
    assert entry_lines(payment.journal_entry) == [("2000", D("100.00"), D("0")), ("2100", D("0"), D("100.00"))]


def test_account_must_fit_the_method(three_bills, admin_user, cash, card):
    a = three_bills[0]
    with pytest.raises(PurchasingError, match="Choose the credit card used."):
        pay_bills(date=d(10, 20), method=M.CARD, paid_from=cash, user=admin_user, lines=[PayLine(a, D("1.00"))])
    with pytest.raises(PurchasingError, match="Choose the bank account to pay from."):
        pay_bills(date=d(10, 20), method=M.ACH, paid_from=card, user=admin_user, lines=[PayLine(a, D("1.00"))])
    with pytest.raises(PurchasingError, match="Choose the bank account to pay from."):
        pay_bills(date=d(10, 20), method=M.CHECK, paid_from=card, user=admin_user, lines=[PayLine(a, D("1.00"))],
                  check_numbers={a.vendor_id: "5"})


def test_check_needs_a_number(three_bills, admin_user, cash):
    a = three_bills[0]
    with pytest.raises(PurchasingError, match="Enter the check number for Acme Supply."):
        pay_bills(date=d(10, 20), method=M.CHECK, paid_from=cash, user=admin_user, lines=[PayLine(a, D("1.00"))])


def test_same_check_number_for_two_vendors_in_one_batch_is_refused(three_bills, admin_user, cash, ap):
    a, _, z = three_bills
    with pytest.raises(PurchasingError, match="Check 1001 is already used on"):
        pay_bills(date=d(10, 20), method=M.CHECK, paid_from=cash, user=admin_user,
                  lines=[PayLine(a, D("10.00")), PayLine(z, D("10.00"))],
                  check_numbers={a.vendor_id: "1001", z.vendor_id: " 1001"})
    assert VendorPayment.objects.count() == 0
    assert BillPaymentApplication.objects.count() == 0
    assert ap_balance(ap) == D("350.00")


def test_check_number_reused_on_the_same_account_is_refused_but_not_on_another(three_bills, admin_user, cash, bank2):
    a, b, z = three_bills
    first = pay_vendor(vendor=a.vendor, date=d(10, 20), method=M.CHECK, amount=D("10.00"), paid_from=cash,
                       user=admin_user, check_number="1001", applications=[(a, D("10.00"))])
    with pytest.raises(PurchasingError, match=r"Check 1001 is already used on 1000 Cash\."):
        pay_vendor(vendor=z.vendor, date=d(10, 21), method=M.CHECK, amount=D("10.00"), paid_from=cash,
                   user=admin_user, check_number="1001")
    pay_vendor(vendor=z.vendor, date=d(10, 21), method=M.CHECK, amount=D("10.00"), paid_from=bank2,
               user=admin_user, check_number="1001")
    void_vendor_payment(first, user=admin_user, reason="Lost in the mail")
    pay_vendor(vendor=z.vendor, date=d(10, 22), method=M.CHECK, amount=D("10.00"), paid_from=cash,
               user=admin_user, check_number="1001")


def test_payment_dated_before_the_bill_is_refused(three_bills, admin_user, cash, ap):
    a = three_bills[0]
    with pytest.raises(PurchasingError, match=f"The date can't be before {a.number}'s date."):
        pay_bills(date=d(10, 4), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(a, D("10.00"))])
    assert VendorPayment.objects.count() == 0


def test_paying_more_than_the_balance_is_refused(three_bills, admin_user, cash):
    a = three_bills[0]
    with pytest.raises(PurchasingError, match=f"{a.number} has 100.00 left to pay."):
        pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(a, D("100.01"))])


def test_paying_the_same_bill_twice_in_a_batch_cannot_overpay(three_bills, admin_user, cash):
    a = three_bills[0]
    with pytest.raises(PurchasingError, match=f"{a.number} has 40.00 left to pay."):
        pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=admin_user,
                  lines=[PayLine(a, D("60.00")), PayLine(a, D("60.00"))])


def test_batch_needs_a_line(setup, admin_user, cash):
    with pytest.raises(PurchasingError, match="Choose at least one bill to pay."):
        pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[])


def test_single_payment_keeps_the_extra_as_unapplied_and_applies_it_later(three_bills, vendor, admin_user, cash, expense):
    a = three_bills[0]
    payment = pay_vendor(vendor=vendor, date=d(10, 20), method=M.ACH, amount=D("130.00"), paid_from=cash,
                         user=admin_user, applications=[(a, D("100.00"))])
    assert vendor_payment_unapplied(payment) == D("30.00")
    assert vendor_balance(vendor) == D("20.00")  # 50.00 open bill minus 30.00 prepayment
    later = bill(vendor, admin_user, expense, "75.00", on=d(10, 25), ref="L")
    with pytest.raises(PurchasingError, match="This payment has 30.00 left to apply."):
        apply_vendor_payment(payment, later, D("31.00"), date=d(10, 26), user=admin_user)
    apply_vendor_payment(payment, later, D("30.00"), date=d(10, 26), user=admin_user)
    assert vendor_payment_unapplied(payment) == D("0.00")
    assert bill_balance(later) == D("45.00")


def test_apply_checks_vendor_and_dates(three_bills, vendor, admin_user, cash):
    a, _, z = three_bills
    payment = pay_vendor(vendor=vendor, date=d(10, 20), method=M.ACH, amount=D("30.00"), paid_from=cash, user=admin_user)
    with pytest.raises(PurchasingError, match=f"{z.number} belongs to a different vendor."):
        apply_vendor_payment(payment, z, D("10.00"), date=d(10, 21), user=admin_user)
    with pytest.raises(PurchasingError, match=f"The date can't be before {payment.number}'s date."):
        apply_vendor_payment(payment, a, D("10.00"), date=d(10, 19), user=admin_user)


def test_payment_dated_before_a_later_bill_cannot_be_applied_to_it(setup, vendor, admin_user, cash, expense):
    payment = pay_vendor(vendor=vendor, date=d(10, 20), method=M.ACH, amount=D("30.00"), paid_from=cash, user=admin_user)
    later = bill(vendor, admin_user, expense, "75.00", on=d(10, 25))
    with pytest.raises(PurchasingError, match=f"The date can't be before {later.number}'s date."):
        apply_vendor_payment(payment, later, D("10.00"), date=d(10, 21), user=admin_user)


def test_remove_application_reopens_the_bill(three_bills, admin_user, cash):
    a = three_bills[0]
    (payment,) = pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(a, D("100.00"))])
    app = payment.applications.get()
    with pytest.raises(PurchasingError, match="The date can't be before the application date."):
        remove_bill_application(app, date=d(10, 19), user=admin_user)
    remove_bill_application(app, date=d(10, 22), user=admin_user)
    assert bill_balance(a) == D("100.00") and vendor_payment_unapplied(payment) == D("100.00")
    with pytest.raises(PurchasingError, match="That application was already removed."):
        remove_bill_application(app, date=d(10, 22), user=admin_user)


def test_void_reopens_the_bills(three_bills, admin_user, cash, ap):
    a, b, _ = three_bills
    (payment,) = pay_bills(date=d(10, 20), method=M.CHECK, paid_from=cash, user=admin_user,
                           lines=[PayLine(a, D("100.00")), PayLine(b, D("50.00"))], check_numbers={a.vendor_id: "7"})
    void_vendor_payment(payment, user=admin_user, reason="Check lost")
    assert payment.status == DocStatus.VOID and payment.voided_on == d(10, 20)
    assert bill_balance(a) == D("100.00") and bill_balance(b) == D("50.00")
    assert ap_balance(ap) == D("350.00")
    assert not payment.applications.filter(removed=False).exists()
    assert JournalEntry.objects.filter(reverses=payment.journal_entry).exists()


def test_void_rules(three_bills, standard_user, admin_user, cash):
    a = three_bills[0]
    (payment,) = pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=admin_user, lines=[PayLine(a, D("10.00"))])
    with pytest.raises(PermissionDenied):
        void_vendor_payment(payment, user=standard_user, reason="x")
    with pytest.raises(PurchasingError, match="Enter a reason."):
        void_vendor_payment(payment, user=admin_user, reason=" ")
    with pytest.raises(PurchasingError, match="The void date can't be before"):
        void_vendor_payment(payment, user=admin_user, reason="x", date=d(10, 10))
    void_vendor_payment(payment, user=admin_user, reason="x")
    with pytest.raises(PurchasingError, match=f"{payment.number} is already void."):
        void_vendor_payment(payment, user=admin_user, reason="x")


def test_standard_user_can_pay_bills(three_bills, standard_user, cash):
    a = three_bills[0]
    (payment,) = pay_bills(date=d(10, 20), method=M.ACH, paid_from=cash, user=standard_user, lines=[PayLine(a, D("10.00"))])
    assert payment.created_by == standard_user

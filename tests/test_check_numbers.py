from datetime import date
from decimal import Decimal

import pytest

from banking import checknumbers
from banking.checknumbers import is_to_print
from banking.models import CheckNumber, CheckStatus
from banking.words import amount_in_words
from purchasing.errors import PurchasingError
from sales.errors import ReceiptError
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.payments import pay_bills, pay_vendor, void_vendor_payment, PayLine
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.models import PaymentMethod
from sales.posting.receipts import issue_refund, receive_payment, void_refund

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.mark.parametrize("amount,words", [
    ("1075.00", "One thousand seventy-five and 00/100"),
    ("0.07", "Zero and 07/100"),
    ("0.00", "Zero and 00/100"),
    ("1000000.00", "One million and 00/100"),
    ("19.99", "Nineteen and 99/100"),
    ("1234567.89", "One million two hundred thirty-four thousand five hundred sixty-seven and 89/100"),
    ("100.00", "One hundred and 00/100"),
    ("21.50", "Twenty-one and 50/100"),
    ("1000.01", "One thousand and 01/100"),
    ("90000.00", "Ninety thousand and 00/100"),
    ("2000300.40", "Two million three hundred and 40/100"),
    ("12.10", "Twelve and 10/100"),
])
def test_amount_in_words(amount, words):
    assert amount_in_words(D(amount)) == words


def test_amount_in_words_rejects_negative():
    with pytest.raises(ValueError):
        amount_in_words(D("-1.00"))


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


def test_normalize_drops_spaces_and_leading_zeros():
    assert checknumbers.normalize(" 002050 ") == "2050"
    assert checknumbers.normalize("0") == "0" and checknumbers.normalize("") == ""


def test_handwritten_number_is_recorded_and_cannot_be_reused(setup, admin_user, bank, checking, vendor):
    p = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")
    row = CheckNumber.objects.get(bank_account=checking)
    assert (row.number, row.status, row.vendor_payment) == ("2050", CheckStatus.HANDWRITTEN, p)
    with pytest.raises(PurchasingError, match=r"Check 002050 was already used on 1000 Cash \(handwritten\)\."):
        pay_vendor(vendor=vendor, date=d(1, 6), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="002050")


def test_voided_payments_number_stays_blocked(setup, admin_user, bank, checking, vendor):
    p = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")
    void_vendor_payment(p, user=admin_user, reason="Lost in the mail")
    assert CheckNumber.objects.get(bank_account=checking).status == CheckStatus.VOIDED
    with pytest.raises(PurchasingError, match=r"Check 2050 was already used on 1000 Cash \(voided\)\."):
        pay_vendor(vendor=vendor, date=d(1, 6), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")


def test_accounts_without_banking_still_block_reuse_from_the_payments_themselves(setup, admin_user, bank, vendor):
    p = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")
    assert not CheckNumber.objects.exists()
    with pytest.raises(PurchasingError, match=r"Check 2050 was already used on 1000 Cash \(handwritten\)\."):
        pay_vendor(vendor=vendor, date=d(1, 6), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")
    void_vendor_payment(p, user=admin_user, reason="x")
    with pytest.raises(PurchasingError, match=r"\(voided\)"):
        pay_vendor(vendor=vendor, date=d(1, 6), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2050")


def test_same_number_is_fine_on_another_bank_account(setup, admin_user, bank, checking, vendor, loan):
    from banking.models import AccountKind, BankAccount
    from ledger.models import Account, AccountSubtype, AccountType

    other = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    BankAccount.objects.create(ledger_account=other, kind=AccountKind.SAVINGS)
    for acct in (bank, other):
        pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=acct,
                   user=admin_user, check_number="2050")
    assert CheckNumber.objects.count() == 2


def test_blank_check_number_is_allowed_and_is_to_print(setup, admin_user, bank, checking, vendor):
    p = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank, user=admin_user)
    assert p.check_number == "" and is_to_print(p)
    assert not CheckNumber.objects.exists()
    handwritten = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                             user=admin_user, check_number="2051")
    ach = pay_vendor(vendor=vendor, date=d(1, 5), method=VM.ACH, amount=D("10.00"), paid_from=bank, user=admin_user)
    assert not is_to_print(handwritten) and not is_to_print(ach)
    void_vendor_payment(p, user=admin_user, reason="x")
    p.refresh_from_db()
    assert not is_to_print(p)


def test_pay_bills_with_blank_numbers_leaves_checks_to_print(setup, admin_user, bank, vendor, expense):
    bill = create_bill(vendor=vendor, date=d(1, 2), vendor_invoice_number="V1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("40.00"))])
    [p] = pay_bills(date=d(1, 5), method=VM.CHECK, paid_from=bank, user=admin_user, lines=[PayLine(bill, D("40.00"))])
    assert is_to_print(p)


def test_same_number_twice_in_one_batch_is_refused(setup, admin_user, bank, checking, vendor, expense):
    from contacts.models import Vendor

    other = Vendor.objects.create(name="Zed Parts")
    a = create_bill(vendor=vendor, date=d(1, 2), vendor_invoice_number="A", user=admin_user,
                    expense_lines=[BillExpenseSpec(expense, D("10.00"))])
    b = create_bill(vendor=other, date=d(1, 2), vendor_invoice_number="B", user=admin_user,
                    expense_lines=[BillExpenseSpec(expense, D("10.00"))])
    with pytest.raises(PurchasingError, match="Check 1001 was already used on 1000 Cash"):
        pay_bills(date=d(1, 5), method=VM.CHECK, paid_from=bank, user=admin_user,
                  lines=[PayLine(a, D("10.00")), PayLine(b, D("10.00"))],
                  check_numbers={vendor.pk: "1001", other.pk: " 1001"})
    assert not CheckNumber.objects.exists()


def test_refund_check_blank_is_to_print_and_numbered_is_registered(setup, admin_user, bank, checking, customer):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    blank = issue_refund(source=pay, date=d(1, 2), bank_account=bank, method="CHECK", amount=D("10.00"),
                         user=admin_user)
    assert is_to_print(blank)
    numbered = issue_refund(source=pay, date=d(1, 3), bank_account=bank, method="CHECK", amount=D("10.00"),
                            user=admin_user, reference="2060")
    assert not is_to_print(numbered)
    assert CheckNumber.objects.get().refund == numbered
    with pytest.raises(ReceiptError, match=r"Check 2060 was already used on 1000 Cash \(handwritten\)\."):
        issue_refund(source=pay, date=d(1, 4), bank_account=bank, method="CHECK", amount=D("10.00"),
                     user=admin_user, reference="2060")
    void_refund(numbered, user=admin_user, reason="x")
    assert CheckNumber.objects.get().status == CheckStatus.VOIDED
    ach = issue_refund(source=pay, date=d(1, 5), bank_account=bank, method="ACH", amount=D("10.00"),
                       user=admin_user, reference="ACH-1")
    assert not is_to_print(ach)


def test_a_refund_check_number_blocks_a_vendor_check_with_the_same_number(setup, admin_user, bank, checking, customer, vendor):
    pay = receive_payment(customer=customer, date=d(1, 1), method=PaymentMethod.ACH, amount=D("100.00"),
                          user=admin_user, bank_account=bank)
    issue_refund(source=pay, date=d(1, 3), bank_account=bank, method="CHECK", amount=D("10.00"),
                 user=admin_user, reference="2060")
    with pytest.raises(PurchasingError, match=r"Check 2060 was already used"):
        pay_vendor(vendor=vendor, date=d(1, 6), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number="2060")


def test_next_number_skips_used_numbers(setup, admin_user, bank, checking, vendor):
    for n in ("2051", "2052"):
        pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
                   user=admin_user, check_number=n)
    CheckNumber.objects.create(bank_account=checking, number="2054", status=CheckStatus.SPOILED)
    assert checknumbers.next_number(checking) == 2053
    CheckNumber.objects.create(bank_account=checking, number="2053", status=CheckStatus.SPOILED)
    assert checknumbers.next_number(checking) == 2055


def test_next_number_skips_legacy_payment_numbers_without_registry_rows(setup, admin_user, bank, checking, vendor):
    pay_vendor(vendor=vendor, date=d(1, 5), method=VM.CHECK, amount=D("10.00"), paid_from=bank,
               user=admin_user, check_number="2051")
    CheckNumber.objects.all().delete()  # as if paid before Phase 5a
    assert checknumbers.next_number(checking) == 2052

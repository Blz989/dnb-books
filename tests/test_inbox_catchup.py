from datetime import date
from decimal import Decimal

import pytest

from banking import inbox
from banking.inbox import InboxError
from banking.models import BankTransaction, Match, TxnSource, TxnStatus
from contacts.models import Vendor
from items.models import ItemType
from items.stock import check_invariants
from purchasing.checks import ap_check
from purchasing.models import VendorPayment, VendorPaymentMethod as VM
from purchasing.posting.bills import BillExpenseSpec, create_bill
from purchasing.posting.payments import void_vendor_payment
from sales.checks import ar_check, undeposited_check
from sales.models import Deposit, DocStatus, Payment, PaymentMethod
from sales.posting.deposits import void_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice
from sales.posting.receipts import void_payment

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def make_txn(checking):
    counter = iter(range(1, 1000))

    def factory(amount, on=d(1, 10), description="Some bank text", account=None, check_number=""):
        return BankTransaction.objects.create(
            bank_account=account or checking, external_id=f"x{next(counter)}", date=on, description=description,
            amount=D(amount), check_number=check_number, source=TxnSource.FILE)

    return factory


@pytest.fixture
def make_bill(setup, vendor, admin_user, expense):
    def factory(amount, on=d(1, 2), who=None, ref="B"):
        return create_bill(vendor=who or vendor, date=on, vendor_invoice_number=ref, user=admin_user,
                           expense_lines=[BillExpenseSpec(expense, D(amount))])
    return factory


@pytest.fixture
def make_invoice(setup, customer, admin_user, service):
    def factory(amount, on=d(1, 2)):
        return create_invoice(customer=customer, date=on, user=admin_user, lines=[InvoiceLineSpec(service, 1, D(amount))])
    return factory


def zero_checks():
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []


def test_pay_bills_exact_total_required(make_bill, make_txn, vendor, admin_user):
    bill = make_bill("1500.00")
    txn = make_txn("-1234.56")
    with pytest.raises(InboxError) as error:
        inbox.pay_bills_from_row(txn, vendor=vendor, amounts={bill.pk: D("1000.00")}, user=admin_user)
    assert str(error.value) == "The bills you chose add up to 1,000.00. The bank shows 1,234.56."
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW and not VendorPayment.objects.exists()


def test_pay_bills_check_number_used(make_bill, make_txn, vendor, admin_user, bank):
    bill = make_bill("420.00")
    txn = make_txn("-420.00", check_number="2050")
    payment = inbox.pay_bills_from_row(txn, vendor=vendor, amounts={bill.pk: D("420.00")}, user=admin_user)
    assert payment.method == VM.CHECK and payment.check_number == "2050"
    assert payment.paid_from == bank and payment.date == txn.date
    from banking.models import CheckNumber
    assert CheckNumber.objects.filter(bank_account=txn.bank_account, number="2050").exists()


def test_pay_bills_card_and_ach(make_bill, make_txn, vendor, admin_user, card_account, checking):
    b1, b2 = make_bill("100.00", ref="B1"), make_bill("50.00", ref="B2")
    ach = inbox.pay_bills_from_row(make_txn("-100.00"), vendor=vendor, amounts={b1.pk: D("100.00")}, user=admin_user)
    card = inbox.pay_bills_from_row(
        make_txn("-50.00", account=card_account), vendor=vendor, amounts={b2.pk: D("50.00")}, user=admin_user)
    assert ach.method == VM.ACH and card.method == VM.CARD and card.paid_from == card_account.ledger_account


def test_pay_bills_check_number_already_used_refused(make_bill, make_txn, vendor, admin_user, bank):
    b1, b2 = make_bill("420.00", ref="B1"), make_bill("300.00", ref="B2")
    inbox.pay_bills_from_row(
        make_txn("-420.00", check_number="2050"), vendor=vendor, amounts={b1.pk: D("420.00")}, user=admin_user)
    second = make_txn("-300.00", check_number="2050")
    with pytest.raises(InboxError) as error:
        inbox.pay_bills_from_row(second, vendor=vendor, amounts={b2.pk: D("300.00")}, user=admin_user)
    assert str(error.value) == f"Check 2050 was already used on {bank} (handwritten)."
    second.refresh_from_db()
    assert second.status == TxnStatus.NEW and not hasattr(second, "match")
    assert VendorPayment.objects.count() == 1
    zero_checks()


def test_pay_bills_links_and_void_returns_row(make_bill, make_txn, vendor, admin_user):
    bill = make_bill("420.00")
    txn = make_txn("-420.00")
    payment = inbox.pay_bills_from_row(txn, vendor=vendor, amounts={bill.pk: D("420.00")}, user=admin_user)
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED and txn.match.vendor_payment == payment
    void_vendor_payment(payment, user=admin_user, reason="Mistake")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW and not Match.objects.exists()


def test_pay_bills_wrong_sign(make_bill, make_txn, vendor, admin_user):
    bill = make_bill("420.00")
    with pytest.raises(InboxError, match="Pay bills is for money going out."):
        inbox.pay_bills_from_row(make_txn("420.00"), vendor=vendor, amounts={bill.pk: D("420.00")}, user=admin_user)
    handled = make_txn("-420.00")
    inbox.exclude(handled, reason="x", user=admin_user)
    with pytest.raises(InboxError, match="This bank transaction was already handled."):
        inbox.pay_bills_from_row(handled, vendor=vendor, amounts={bill.pk: D("420.00")}, user=admin_user)


def test_receive_payment_ach(make_invoice, make_txn, customer, admin_user, bank):
    inv = make_invoice("500.00")
    txn = make_txn("500.00")
    payment = inbox.receive_payment_from_row(
        txn, customer=customer, amounts={inv.pk: D("500.00")}, method=PaymentMethod.ACH, user=admin_user)
    txn.refresh_from_db()
    assert payment.deposited_to == bank and payment.date == txn.date
    assert txn.status == TxnStatus.MATCHED and txn.match.sales_payment == payment
    void_payment(payment, user=admin_user, reason="Mistake")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW


def test_receive_payment_check_makes_one_payment_deposit(make_invoice, make_txn, customer, admin_user, bank):
    inv = make_invoice("500.00")
    txn = make_txn("500.00", check_number="778")
    payment = inbox.receive_payment_from_row(
        txn, customer=customer, amounts={inv.pk: D("500.00")}, method=PaymentMethod.CHECK, user=admin_user)
    txn.refresh_from_db()
    deposit = Deposit.objects.get()
    payment.refresh_from_db()
    assert payment.method == PaymentMethod.CHECK and payment.deposit == deposit
    assert deposit.bank_account == bank and deposit.date == txn.date
    assert txn.status == TxnStatus.MATCHED and txn.match.deposit == deposit
    assert undeposited_check() == []
    void_deposit(deposit, user=admin_user, reason="Mistake")
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW


def test_receive_payment_total_mismatch(make_invoice, make_txn, customer, admin_user, card_account):
    inv = make_invoice("5000.00")
    txn = make_txn("1234.56")
    with pytest.raises(InboxError) as error:
        inbox.receive_payment_from_row(
            txn, customer=customer, amounts={inv.pk: D("1000.00")}, method=PaymentMethod.ACH, user=admin_user)
    assert str(error.value) == "The invoices you chose add up to 1,000.00. The bank shows 1,234.56."
    with pytest.raises(InboxError, match="Receive payment is for money coming in."):
        inbox.receive_payment_from_row(
            make_txn("-5.00"), customer=customer, amounts={inv.pk: D("5.00")}, method=PaymentMethod.ACH, user=admin_user)
    with pytest.raises(InboxError, match="Receive payment is for money coming in."):
        inbox.receive_payment_from_row(
            make_txn("5.00", account=card_account), customer=customer, amounts={inv.pk: D("5.00")},
            method=PaymentMethod.ACH, user=admin_user)
    assert not Payment.objects.exists()


def test_fill_oldest_first():
    docs = [(1, D("100.00")), (2, D("50.00")), (3, D("75.00"))]
    assert inbox.fill_oldest_first(docs, D("120.00")) == {1: D("100.00"), 2: D("20.00")}
    assert inbox.fill_oldest_first(docs, D("225.00")) == {1: D("100.00"), 2: D("50.00"), 3: D("75.00")}
    assert inbox.fill_oldest_first(docs, D("500.00")) == {1: D("100.00"), 2: D("50.00"), 3: D("75.00")}
    assert inbox.fill_oldest_first(docs, D("0.00")) == {} and inbox.fill_oldest_first([], D("10.00")) == {}


def test_ledger_checks_zero(make_bill, make_invoice, make_txn, vendor, customer, admin_user):
    bill = make_bill("420.00")
    inv, inv2 = make_invoice("500.00"), make_invoice("300.00")
    inbox.pay_bills_from_row(make_txn("-420.00", check_number="9"), vendor=vendor, amounts={bill.pk: D("420.00")}, user=admin_user)
    inbox.receive_payment_from_row(
        make_txn("500.00"), customer=customer, amounts={inv.pk: D("500.00")}, method=PaymentMethod.WIRE, user=admin_user)
    inbox.receive_payment_from_row(
        make_txn("300.00"), customer=customer, amounts={inv2.pk: D("300.00")}, method=PaymentMethod.CHECK, user=admin_user)
    zero_checks()

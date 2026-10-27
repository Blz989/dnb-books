from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from contacts.models import Customer
from items.models import ItemType
from ledger.balances import account_totals
from ledger.models import EntryStatus
from sales.balances import (
    credit_unapplied, customer_balance, invoice_balance, payment_unapplied,
)
from sales.errors import ReceiptError, SalesSettingsMissing
from sales.models import DocStatus, PaymentMethod, RefundMethod
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.receipts import (
    CreditLineSpec, apply_credit, apply_payment, create_credit_note, issue_refund, oldest_first, receive_payment,
    remove_application, void_credit_note, void_payment, void_refund,
)

D = Decimal


def d(month, day):
    return date(2026, month, day)


def net(account):
    debit, credit = account_totals(start=None, end=d(12, 31)).get(account.pk, (D("0"), D("0")))
    return debit - credit


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def make_invoice(service, customer, admin_user):
    def factory(amount, day=1, who=None):
        return create_invoice(customer=who or customer, date=d(10, day), user=admin_user,
                              lines=[InvoiceLineSpec(service, 1, D(amount))])
    return factory


@pytest.fixture
def inv100(make_invoice):
    return make_invoice("100.00")


def pay(customer, user, amount="100.00", method=PaymentMethod.CHECK, day=5, **kw):
    return receive_payment(customer=customer, date=d(10, day), method=method, amount=D(amount), user=user, **kw)


def test_check_goes_to_undeposited_funds(customer, admin_user, sales_settings, ar):
    p = pay(customer, admin_user, reference="1042")
    assert p.number == "PMT-00001" and p.deposited_to == sales_settings.undeposited_account and p.reference == "1042"
    assert entry_lines(p.journal_entry) == [("1200", D("0"), D("100.00")), ("1250", D("100.00"), D("0"))]
    assert (p.journal_entry.source_type, p.journal_entry.source_id) == ("sales.payment", "PMT-00001")


def test_cash_also_goes_to_undeposited(customer, admin_user, sales_settings):
    assert pay(customer, admin_user, method=PaymentMethod.CASH).deposited_to == sales_settings.undeposited_account


def test_ach_needs_a_bank_account(customer, admin_user, bank, ar, sales_settings):
    with pytest.raises(ReceiptError, match="Choose the bank account the ACH went to."):
        pay(customer, admin_user, method=PaymentMethod.ACH)
    with pytest.raises(ReceiptError, match="Choose the bank account the Wire went to."):
        pay(customer, admin_user, method=PaymentMethod.WIRE, bank_account=ar)
    p = pay(customer, admin_user, method=PaymentMethod.ACH, bank_account=bank)
    assert p.deposited_to == bank and net(bank) == D("100.00")


def test_amount_rules(customer, admin_user, sales_settings):
    with pytest.raises(ReceiptError, match="Enter an amount above zero."):
        pay(customer, admin_user, amount="0.00")
    with pytest.raises(ReceiptError, match="Amounts can have at most 2 decimal places."):
        pay(customer, admin_user, amount="1.005")


def test_settings_missing(customer, admin_user, db):
    with pytest.raises(SalesSettingsMissing, match="Set up sales accounts first."):
        pay(customer, admin_user)


def test_payment_applied_at_receipt(customer, admin_user, make_invoice):
    a, b = make_invoice("60.00"), make_invoice("50.00", day=2)
    p = pay(customer, admin_user, amount="110.00", applications=[(a, D("60.00")), (b, D("50.00"))])
    assert invoice_balance(a) == invoice_balance(b) == D("0.00") and payment_unapplied(p) == D("0.00")
    assert all(x.applied_on == d(10, 5) for x in p.applications.all())


def test_overpayment_leaves_credit_applied_later(customer, admin_user, make_invoice, inv100):
    p = pay(customer, admin_user, amount="130.00", applications=[(inv100, D("100.00"))])
    assert payment_unapplied(p) == D("30.00") and customer_balance(customer) == D("-30.00")
    later = make_invoice("40.00", day=8)
    app = apply_payment(p, later, D("30.00"), date=d(10, 9), user=admin_user)
    assert app.applied_on == d(10, 9) and invoice_balance(later) == D("10.00") and payment_unapplied(p) == D("0.00")


def test_cannot_apply_more_than_invoice_balance(customer, admin_user, inv100):
    with pytest.raises(ReceiptError, match=r"INV-000001 has 100\.00 left to pay\."):
        pay(customer, admin_user, amount="150.00", applications=[(inv100, D("120.00"))])
    p = pay(customer, admin_user, amount="150.00")
    with pytest.raises(ReceiptError, match=r"INV-000001 has 100\.00 left to pay\."):
        apply_payment(p, inv100, D("100.01"), date=d(10, 6), user=admin_user)


def test_cannot_apply_more_than_the_payment_has(customer, admin_user, make_invoice, inv100):
    p = pay(customer, admin_user, amount="60.00", applications=[(inv100, D("40.00"))])
    with pytest.raises(ReceiptError, match=r"This payment has 20\.00 left to apply\."):
        apply_payment(p, inv100, D("25.00"), date=d(10, 6), user=admin_user)
    with pytest.raises(ReceiptError, match=r"This payment has 60\.00 left to apply\."):
        pay(customer, admin_user, amount="60.00", applications=[(make_invoice("90.00"), D("70.00"))])


def test_application_must_be_same_customer_and_open(customer, admin_user, make_invoice):
    other = Customer.objects.create(name="Other Co")
    theirs = make_invoice("10.00", who=other)
    with pytest.raises(ReceiptError, match="INV-000001 belongs to a different customer."):
        pay(customer, admin_user, applications=[(theirs, D("10.00"))])
    mine = make_invoice("10.00")
    void_invoice(mine, user=admin_user, reason="Oops")
    with pytest.raises(ReceiptError, match=r"INV-000002 is void\."):
        pay(customer, admin_user, applications=[(mine, D("5.00"))])


def test_application_date_cannot_precede_documents(customer, admin_user, make_invoice):
    late = make_invoice("10.00", day=20)
    with pytest.raises(ReceiptError, match="The date can't be before INV-000001's date."):
        pay(customer, admin_user, amount="10.00", applications=[(late, D("10.00"))])


def test_removing_an_application_keeps_history(customer, admin_user, inv100):
    p = pay(customer, admin_user, applications=[(inv100, D("100.00"))])
    (app,) = p.applications.all()
    remove_application(app, date=d(10, 9), user=admin_user)
    app.refresh_from_db()
    assert app.removed and app.removed_on == d(10, 9)
    assert invoice_balance(inv100) == D("100.00") and invoice_balance(inv100, d(10, 7)) == D("0.00")
    with pytest.raises(ReceiptError, match="That application was already removed."):
        remove_application(app, date=d(10, 10), user=admin_user)


def test_cannot_remove_application_before_it_was_applied(customer, admin_user, inv100):
    p = pay(customer, admin_user, applications=[(inv100, D("100.00"))])
    (app,) = p.applications.all()
    with pytest.raises(ReceiptError, match="The date can't be before the application date."):
        remove_application(app, date=d(10, 4), user=admin_user)


def test_oldest_first(customer, admin_user, make_invoice):
    newer = make_invoice("50.00", day=9)
    older = make_invoice("70.00", day=2)
    got = oldest_first(customer, D("100.00"))
    assert got == [(older, D("70.00")), (newer, D("30.00"))]
    assert oldest_first(customer, D("500.00")) == [(older, D("70.00")), (newer, D("50.00"))]
    pay(customer, admin_user, amount="70.00", applications=[(older, D("70.00"))])
    assert oldest_first(customer, D("20.00")) == [(newer, D("20.00"))]


def test_credit_note_posts_and_applies(customer, admin_user, inv100, sales_settings, ar):
    cn = create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="Damaged goods",
                            lines=[CreditLineSpec("Damaged", D("30.00")), CreditLineSpec("Freight", D("5.00"), account=sales_settings.returns_account)])
    assert cn.number == "CN-00001" and credit_unapplied(cn) == D("35.00")
    assert entry_lines(cn.journal_entry) == sorted(
        [("1200", D("0"), D("35.00")), ("4900", D("30.00"), D("0")), ("4900", D("5.00"), D("0"))])
    app = apply_credit(cn, inv100, D("35.00"), date=d(10, 7), user=admin_user)
    assert invoice_balance(inv100) == D("65.00") and credit_unapplied(cn) == D("0.00") and app.removed is False
    with pytest.raises(ReceiptError, match=r"This credit note has 0\.00 left to apply\."):
        apply_credit(cn, inv100, D("1.00"), date=d(10, 8), user=admin_user)
    remove_application(app, date=d(10, 9), user=admin_user)
    assert credit_unapplied(cn) == D("35.00")


def test_credit_note_validation(customer, admin_user, sales_settings, ar):
    with pytest.raises(ReceiptError, match="Add at least one line."):
        create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[])
    with pytest.raises(ReceiptError, match="Line 1: enter an amount above zero."):
        create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("0"))])
    with pytest.raises(ReceiptError, match="Line 1: choose an account other than accounts receivable."):
        create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("1"), account=ar)])


def test_refund_from_payment_unapplied(customer, admin_user, inv100, bank, ar):
    p = pay(customer, admin_user, amount="130.00", applications=[(inv100, D("100.00"))])
    r = issue_refund(source=p, date=d(10, 8), bank_account=bank, method=RefundMethod.CHECK, amount=D("20.00"),
                     user=admin_user, reference="2001")
    assert r.number == "REF-00001" and r.customer == customer and payment_unapplied(p) == D("10.00")
    assert entry_lines(r.journal_entry) == [("1000", D("0"), D("20.00")), ("1200", D("20.00"), D("0"))]
    with pytest.raises(ReceiptError, match=r"Only 10\.00 is available to refund from PMT-00001\."):
        issue_refund(source=p, date=d(10, 9), bank_account=bank, method=RefundMethod.ACH, amount=D("10.01"), user=admin_user)


def test_refund_from_credit_note_and_admin_only(customer, admin_user, standard_user, bank, sales_settings):
    cn = create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("25.00"))])
    with pytest.raises(PermissionDenied):
        issue_refund(source=cn, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("5.00"), user=standard_user)
    r = issue_refund(source=cn, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("25.00"), user=admin_user)
    assert r.source_credit_note == cn and credit_unapplied(cn) == D("0.00")


def test_refund_needs_bank_and_amount(customer, admin_user, bank, ar, sales_settings):
    p = pay(customer, admin_user)
    with pytest.raises(ReceiptError, match="Choose the bank account the refund comes from."):
        issue_refund(source=p, date=d(10, 8), bank_account=ar, method=RefundMethod.ACH, amount=D("5.00"), user=admin_user)
    with pytest.raises(ReceiptError, match="Enter an amount above zero."):
        issue_refund(source=p, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("0"), user=admin_user)


def test_void_payment_removes_applications_and_reopens_invoices(customer, admin_user, inv100, ar, sales_settings):
    p = pay(customer, admin_user, applications=[(inv100, D("100.00"))])
    out = void_payment(p, user=admin_user, reason="Check bounced", date=d(10, 12))
    assert out.status == DocStatus.VOID and out.voided_on == d(10, 12) and out.number == "PMT-00001"
    assert invoice_balance(inv100) == D("100.00") and invoice_balance(inv100, d(10, 11)) == D("0.00")
    (app,) = p.applications.all()
    assert app.removed and app.removed_on == d(10, 12)
    assert net(ar) == D("100.00") and customer_balance(customer) == D("100.00")
    p.journal_entry.refresh_from_db()
    assert p.journal_entry.status == EntryStatus.VOID


def test_void_payment_default_date_follows_latest_application(customer, admin_user, inv100):
    p = pay(customer, admin_user, amount="130.00")
    apply_payment(p, inv100, D("100.00"), date=d(10, 9), user=admin_user)
    out = void_payment(p, user=admin_user, reason="Bounced")
    assert out.voided_on == d(10, 9)
    with pytest.raises(ReceiptError):
        void_payment(pay(customer, admin_user, day=6), user=admin_user, reason="x", date=d(10, 5))


def test_void_payment_blocked_by_refund(customer, admin_user, bank, sales_settings):
    p = pay(customer, admin_user)
    r = issue_refund(source=p, date=d(10, 8), bank_account=bank, method=RefundMethod.ACH, amount=D("10.00"), user=admin_user)
    with pytest.raises(ReceiptError, match="PMT-00001 has a refund drawn from it. Void the refund first."):
        void_payment(p, user=admin_user, reason="x")
    void_refund(r, user=admin_user, reason="Mistake")
    void_payment(p, user=admin_user, reason="x")


def test_void_credit_note_rules(customer, admin_user, inv100, bank, sales_settings):
    cn = create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="x", lines=[CreditLineSpec("a", D("30.00"))])
    app = apply_credit(cn, inv100, D("10.00"), date=d(10, 7), user=admin_user)
    with pytest.raises(ReceiptError, match="CN-00001 is applied to invoices. Remove the applications first."):
        void_credit_note(cn, user=admin_user, reason="x")
    remove_application(app, date=d(10, 8), user=admin_user)
    r = issue_refund(source=cn, date=d(10, 9), bank_account=bank, method=RefundMethod.ACH, amount=D("5.00"), user=admin_user)
    with pytest.raises(ReceiptError, match="CN-00001 has a refund drawn from it. Void the refund first."):
        void_credit_note(cn, user=admin_user, reason="x")
    void_refund(r, user=admin_user, reason="x")
    out = void_credit_note(cn, user=admin_user, reason="Entered twice", date=d(10, 10))
    assert out.status == DocStatus.VOID and credit_unapplied(cn) == D("0.00")


def test_void_refund_restores_unapplied(customer, admin_user, bank, sales_settings):
    p = pay(customer, admin_user)
    r = issue_refund(source=p, date=d(10, 8), bank_account=bank, method=RefundMethod.CHECK, amount=D("40.00"), user=admin_user)
    assert payment_unapplied(p) == D("60.00")
    out = void_refund(r, user=admin_user, reason="Check never sent")
    assert out.status == DocStatus.VOID and payment_unapplied(p) == D("100.00")
    with pytest.raises(ReceiptError, match="REF-00001 is already void."):
        void_refund(r, user=admin_user, reason="again")


def test_voids_are_admin_only_and_need_reason(customer, admin_user, standard_user, sales_settings):
    p = pay(customer, admin_user)
    with pytest.raises(PermissionDenied):
        void_payment(p, user=standard_user, reason="x")
    with pytest.raises(ReceiptError, match="Enter a reason."):
        void_payment(p, user=admin_user, reason=" ")


def test_standard_user_can_receive_and_apply(customer, standard_user, inv100):
    p = pay(customer, standard_user, applications=[(inv100, D("100.00"))])
    assert invoice_balance(inv100) == D("0.00") and p.created_by == standard_user

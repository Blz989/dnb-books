from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from hypothesis import given, settings as hyp_settings, strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase

from contacts.models import Customer
from core.roles import ADMIN_GROUP
from items.errors import StockError
from items.models import Item, ItemType
from ledger.errors import PeriodCloseBlockedError, PostingError
from ledger.models import Account, AccountSubtype, AccountType
from ledger.periods import close_period
from ledger.posting import LineSpec, post_entry
from sales.checks import ar_check, close_check, undeposited_check
from sales.errors import SalesError
from sales.models import (
    CreditNote, DocStatus, Deposit, Invoice, Payment, PaymentApplication, CreditApplication, PaymentMethod, Refund,
    RefundMethod, SalesSettings,
)
from sales.balances import credit_unapplied, invoice_balance, payment_unapplied
from sales.posting.deposits import make_deposit, undeposited_payments, void_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, void_invoice
from sales.posting.receipts import (
    CreditLineSpec, apply_credit, apply_payment, create_credit_note, issue_refund, oldest_first, receive_payment,
    remove_application, void_credit_note, void_payment, void_refund,
)

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def mixed(service, customer, admin_user, bank):
    inv = create_invoice(customer=customer, date=d(10, 1), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("500.00"))])
    check = receive_payment(customer=customer, date=d(10, 3), method=PaymentMethod.CHECK, amount=D("200.00"), user=admin_user,
                            applications=[(inv, D("150.00"))])
    ach = receive_payment(customer=customer, date=d(10, 4), method=PaymentMethod.ACH, amount=D("100.00"), user=admin_user,
                          bank_account=bank)
    make_deposit(payments=[check], bank_account=bank, date=d(10, 6), user=admin_user)
    cn = create_credit_note(customer=customer, date=d(10, 7), user=admin_user, reason="x", lines=[CreditLineSpec("Return", D("40.00"))])
    apply_credit(cn, inv, D("25.00"), date=d(10, 8), user=admin_user)
    issue_refund(source=ach, date=d(10, 9), bank_account=bank, method=RefundMethod.ACH, amount=D("30.00"), user=admin_user)
    return inv


def test_both_checks_are_empty_after_a_mixed_scenario(mixed):
    assert ar_check() == [] and undeposited_check() == []
    for day in range(1, 31):
        assert ar_check(d(10, day)) == [] and undeposited_check(d(10, day)) == []


def test_checks_are_quiet_without_settings(db):
    assert ar_check() == [] and undeposited_check() == []


def test_manual_entry_to_ar_is_reported(mixed, ar, revenue, admin_user):
    post_entry(date=d(10, 10), user=admin_user, lines=[LineSpec(ar, debit=D("12.34")), LineSpec(revenue, credit=D("12.34"))])
    assert ar_check() == ["Customer balances say 190.00, the A/R account says 202.34."]
    assert ar_check(d(10, 9)) == []
    assert ar_check(d(10, 10)) != []


def test_ar_message_format(mixed, ar, revenue, admin_user):
    post_entry(date=d(10, 10), user=admin_user, lines=[LineSpec(ar, debit=D("1000.00")), LineSpec(revenue, credit=D("1000.00"))])
    (message,) = ar_check()
    assert message == "Customer balances say 190.00, the A/R account says 1,190.00."


def test_manual_entry_to_undeposited_is_reported(mixed, sales_settings, revenue, admin_user):
    post_entry(date=d(10, 10), user=admin_user, lines=[
        LineSpec(sales_settings.undeposited_account, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    assert undeposited_check() == ["Undeposited payments say 0.00, the Undeposited Funds account says 5.00."]


def test_mismatch_blocks_close_and_admin_can_override(mixed, ar, revenue, admin_user):
    post_entry(date=d(10, 10), user=admin_user, lines=[LineSpec(ar, debit=D("1.00")), LineSpec(revenue, credit=D("1.00"))])
    assert close_check(2026, 10)[0].startswith("October 2026: Customer balances say")
    with pytest.raises(PeriodCloseBlockedError, match="October 2026: Customer balances say"):
        close_period(2026, 10, admin_user)
    p = close_period(2026, 10, admin_user, override_reason="Known manual entry")
    assert p.is_closed and "Customer balances say" in p.close_override_problems


def test_unpaid_invoices_and_undeposited_checks_do_not_block_close(mixed, admin_user):
    assert close_check(2026, 10) == []
    assert close_period(2026, 10, admin_user).is_closed


def test_close_checks_a_past_month_as_of_its_end(mixed, customer, admin_user, service, ar, revenue):
    create_invoice(customer=customer, date=d(11, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("10.00"))])
    assert close_check(2026, 10) == []
    assert close_check(2026, 11) == []


def test_edit_date_after_an_application_is_refused(mixed, service, admin_user):
    from sales.errors import InvoiceError

    with pytest.raises(InvoiceError, match="The invoice date can't be after 2026-10-03"):
        edit_invoice(mixed, user=admin_user, date=d(10, 5), terms=mixed.terms, ship_to="", notes="",
                     lines=[InvoiceLineSpec(service, 1, D("500.00"))])


class SalesChecksHoldUnderRandomOperations(HypothesisTestCase):
    def setUp(self):
        call_command("setup_roles", verbosity=0)
        self.user = get_user_model().objects.create_user("rnd", password="pw")
        self.user.groups.add(Group.objects.get(name=ADMIN_GROUP))

        def acct(n, name, t, s):
            return Account.objects.create(number=n, name=name, type=t, subtype=s)

        ar = acct("1200", "A/R", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE)
        undeposited = acct("1250", "Undeposited", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET)
        returns = acct("4900", "Returns", AccountType.INCOME, AccountSubtype.INCOME)
        self.bank = acct("1000", "Bank", AccountType.ASSET, AccountSubtype.BANK)
        sales = acct("4000", "Sales", AccountType.INCOME, AccountSubtype.INCOME)
        s = SalesSettings.load()
        s.ar_account, s.undeposited_account, s.returns_account = ar, undeposited, returns
        s.save()
        self.item = Item.objects.create(sku="SVC", name="Service", item_type=ItemType.NON_STOCK, income_account=sales)
        self.customers = [Customer.objects.create(name="One Co"), Customer.objects.create(name="Two Co")]

    def pick(self, queryset, n):
        rows = list(queryset)
        return rows[n % len(rows)] if rows else None

    def apply_op(self, op, a, b, day, cents):
        u, amount = self.user, D(cents) / D(100)
        when = d(10, day)
        if op == 0:
            create_invoice(customer=self.customers[a % 2], date=when, user=u, lines=[InvoiceLineSpec(self.item, 1, amount)])
        elif op == 1:
            inv = self.pick(Invoice.objects.filter(status=DocStatus.OPEN), a)
            if inv:
                edit_invoice(inv, user=u, date=when if b % 2 else inv.date, terms=inv.terms, ship_to="", notes="",
                             lines=[InvoiceLineSpec(self.item, 1, amount)])
        elif op == 2:
            method = [PaymentMethod.CHECK, PaymentMethod.CASH, PaymentMethod.ACH][b % 3]
            customer = self.customers[a % 2]
            apps = oldest_first(customer, amount / 2) if day % 2 else []
            receive_payment(customer=customer, date=when, method=method, amount=amount, user=u,
                            bank_account=self.bank, applications=apps)
        elif op == 3:
            pay = self.pick(Payment.objects.filter(status=DocStatus.OPEN), a)
            inv = self.pick(Invoice.objects.filter(status=DocStatus.OPEN, customer=pay.customer), b) if pay else None
            if pay and inv:
                apply_payment(pay, inv, min(amount, payment_unapplied(pay), invoice_balance(inv)), date=when, user=u)
        elif op == 4:
            apps = list(PaymentApplication.objects.filter(removed=False)) + list(CreditApplication.objects.filter(removed=False))
            if apps:
                remove_application(apps[a % len(apps)], date=when, user=u)
        elif op == 5:
            create_credit_note(customer=self.customers[a % 2], date=when, user=u, reason="r", lines=[CreditLineSpec("x", amount)])
        elif op == 6:
            cn = self.pick(CreditNote.objects.filter(status=DocStatus.OPEN), a)
            inv = self.pick(Invoice.objects.filter(status=DocStatus.OPEN, customer=cn.customer), b) if cn else None
            if cn and inv:
                apply_credit(cn, inv, min(amount, credit_unapplied(cn), invoice_balance(inv)), date=when, user=u)
        elif op == 7:
            sources = list(Payment.objects.filter(status=DocStatus.OPEN)) + list(CreditNote.objects.filter(status=DocStatus.OPEN))
            if sources:
                src = sources[a % len(sources)]
                left = payment_unapplied(src) if isinstance(src, Payment) else credit_unapplied(src)
                issue_refund(source=src, date=when, bank_account=self.bank, method=RefundMethod.ACH, amount=min(amount, left), user=u)
        elif op == 8:
            pool = list(undeposited_payments())
            if pool:
                make_deposit(payments=pool[: 1 + a % len(pool)], bank_account=self.bank, date=when, user=u)
        else:
            kind = b % 5
            use_date = when if day % 3 == 0 else None
            if kind == 0:
                doc = self.pick(Invoice.objects.filter(status=DocStatus.OPEN), a)
                doc and void_invoice(doc, user=u, reason="r", date=use_date)
            elif kind == 1:
                doc = self.pick(Payment.objects.filter(status=DocStatus.OPEN), a)
                doc and void_payment(doc, user=u, reason="r", date=use_date)
            elif kind == 2:
                doc = self.pick(CreditNote.objects.filter(status=DocStatus.OPEN), a)
                doc and void_credit_note(doc, user=u, reason="r", date=use_date)
            elif kind == 3:
                doc = self.pick(Refund.objects.filter(status=DocStatus.OPEN), a)
                doc and void_refund(doc, user=u, reason="r", date=use_date)
            else:
                doc = self.pick(Deposit.objects.filter(status=DocStatus.OPEN), a)
                doc and void_deposit(doc, user=u, reason="r", date=use_date)

    @given(ops=st.lists(
        st.tuples(st.integers(0, 9), st.integers(0, 7), st.integers(0, 7), st.integers(1, 28), st.integers(100, 50000)),
        min_size=1, max_size=14))
    @hyp_settings(max_examples=60, deadline=None)
    def test_random_operations_keep_both_checks_at_zero(self, ops):
        for op, a, b, day, cents in ops:
            try:
                self.apply_op(op, a, b, day, cents)
            except (SalesError, PostingError, StockError, PermissionDenied, ValueError):
                continue
            assert ar_check() == [] and undeposited_check() == [], (op, a, b, day, cents)
        for day in range(1, 31):
            assert ar_check(d(10, day)) == [], day
            assert undeposited_check(d(10, day)) == [], day

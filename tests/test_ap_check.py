from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from hypothesis import given, settings as hyp_settings, strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase

from contacts.models import Customer, Vendor
from core.roles import ADMIN_GROUP
from items.errors import StockError
from items.models import CostingMethod, InventorySettings, Item, ItemType
from items.stock import check_invariants
from ledger.errors import PeriodCloseBlockedError, PostingError
from ledger.models import Account, AccountSubtype, AccountType
from ledger.periods import close_period
from ledger.posting import LineSpec, post_entry
from purchasing.balances import bill_balance, vendor_credit_unapplied, vendor_payment_unapplied
from purchasing.checks import ap_check, close_check
from purchasing.errors import PurchasingError
from purchasing.models import (
    Bill, BillLineKind, BillPaymentApplication, PurchaseOrder, PurchasingSettings, VendorCredit,
    VendorCreditApplication, VendorPayment, VendorPaymentMethod as M,
)
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, bill_from_po, create_bill, edit_bill, void_bill
from purchasing.posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, apply_vendor_credit, create_allowance_credit, create_return_credit,
    void_vendor_credit,
)
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po, po_remaining
from purchasing.posting.payments import (
    PayLine, apply_vendor_payment, pay_bills, pay_vendor, remove_bill_application, void_vendor_payment,
)
from sales.checks import ar_check, undeposited_check
from sales.errors import SalesError
from sales.models import DocStatus, Fulfillment, Invoice, SalesOrder, SalesSettings
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.5000", sale_price="2.0000")


@pytest.fixture
def mixed(setup, widget, vendor, admin_user, freight, cash, card):
    """A PO received in two bills, an expense bill, three payments (one partial), a return credit applied."""
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 1000, D("0.50"))])
    line = po.lines.get()
    first = bill_from_po(po, date=d(10, 3), vendor_invoice_number="V1", quantities={line.pk: 400},
                         prices={line.pk: D("0.48")}, user=admin_user,
                         expense_lines=[BillExpenseSpec(freight, D("25.00"))])
    second = bill_from_po(po, date=d(10, 4), vendor_invoice_number="V2", quantities={line.pk: 600}, user=admin_user)
    rent = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="RENT", user=admin_user,
                       expense_lines=[BillExpenseSpec(freight, D("80.00"))])
    pay_vendor(vendor=vendor, date=d(10, 10), method=M.CHECK, amount=D("100.00"), paid_from=cash, user=admin_user,
               check_number="501", applications=[(first, D("100.00"))])
    pay_vendor(vendor=vendor, date=d(10, 11), method=M.ACH, amount=D("300.00"), paid_from=cash, user=admin_user,
               applications=[(second, D("300.00"))])
    pay_vendor(vendor=vendor, date=d(10, 12), method=M.CARD, amount=D("80.00"), paid_from=card, user=admin_user,
               applications=[(rent, D("80.00"))])
    credit = create_return_credit(vendor=vendor, date=d(10, 15), user=admin_user, reason="Damaged",
                                  lines=[ReturnLineSpec(widget, 10, D("5.00"))])
    apply_vendor_credit(credit, first, D("5.00"), date=d(10, 16), user=admin_user)
    return first


def test_check_is_empty_after_a_mixed_scenario(mixed):
    assert ap_check() == []
    for day in range(1, 31):
        assert ap_check(d(10, day)) == []
    assert check_invariants() == []


def test_check_is_quiet_without_settings(db):
    assert ap_check() == []


def test_manual_entry_to_ap_is_reported(mixed, ap, expense, admin_user):
    post_entry(date=d(10, 20), user=admin_user, lines=[LineSpec(expense, debit=D("12.34")), LineSpec(ap, credit=D("12.34"))])
    assert ap_check() == ["Vendor balances say 112.00, the A/P account says 124.34."]
    assert ap_check(d(10, 19)) == []
    assert ap_check(d(10, 20)) != []


def test_ap_message_format(setup, vendor, admin_user, freight, ap, expense):
    create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="R", user=admin_user,
                expense_lines=[BillExpenseSpec(freight, D("100.00"))])
    post_entry(date=d(10, 6), user=admin_user, lines=[LineSpec(expense, debit=D("1000.00")), LineSpec(ap, credit=D("1000.00"))])
    assert ap_check() == ["Vendor balances say 100.00, the A/P account says 1,100.00."]


def test_mismatch_blocks_close_and_admin_can_override(mixed, ap, expense, admin_user):
    post_entry(date=d(10, 20), user=admin_user, lines=[LineSpec(expense, debit=D("1.00")), LineSpec(ap, credit=D("1.00"))])
    assert close_check(2026, 10)[0].startswith("October 2026: Vendor balances say")
    with pytest.raises(PeriodCloseBlockedError, match="October 2026: Vendor balances say"):
        close_period(2026, 10, admin_user)
    p = close_period(2026, 10, admin_user, override_reason="Known manual entry")
    assert p.is_closed and "Vendor balances say" in p.close_override_problems


def test_unpaid_bills_do_not_block_close(mixed, admin_user):
    assert close_check(2026, 10) == []
    assert close_period(2026, 10, admin_user).is_closed


class PurchasingChecksHoldUnderRandomOperations(HypothesisTestCase):
    def setUp(self):
        call_command("setup_roles", verbosity=0)
        self.user = get_user_model().objects.create_user("rnd", password="pw")
        self.user.groups.add(Group.objects.get(name=ADMIN_GROUP))

        def acct(n, name, t, s):
            return Account.objects.create(number=n, name=name, type=t, subtype=s)

        ar = acct("1200", "A/R", AccountType.ASSET, AccountSubtype.ACCOUNTS_RECEIVABLE)
        undeposited = acct("1250", "Undeposited", AccountType.ASSET, AccountSubtype.OTHER_CURRENT_ASSET)
        returns = acct("4900", "Returns", AccountType.INCOME, AccountSubtype.INCOME)
        sales = acct("4000", "Sales", AccountType.INCOME, AccountSubtype.INCOME)
        ap = acct("2000", "A/P", AccountType.LIABILITY, AccountSubtype.ACCOUNTS_PAYABLE)
        inventory = acct("1300", "Inventory", AccountType.ASSET, AccountSubtype.INVENTORY)
        cogs = acct("5000", "COGS", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
        shrink = acct("6500", "Shrinkage", AccountType.EXPENSE, AccountSubtype.EXPENSE)
        writedown = acct("6510", "Write-down", AccountType.EXPENSE, AccountSubtype.EXPENSE)
        opening = acct("3900", "Opening", AccountType.EQUITY, AccountSubtype.EQUITY)
        self.bank = acct("1000", "Bank", AccountType.ASSET, AccountSubtype.BANK)
        self.card = acct("2100", "Visa", AccountType.LIABILITY, AccountSubtype.CREDIT_CARD)
        self.freight = acct("6150", "Freight", AccountType.EXPENSE, AccountSubtype.EXPENSE)
        s = SalesSettings.load()
        s.ar_account, s.undeposited_account, s.returns_account = ar, undeposited, returns
        s.save()
        p = PurchasingSettings.load()
        p.ap_account = ap
        p.save()
        inv = InventorySettings.load()
        inv.default_inventory_account, inv.default_cogs_account, inv.default_income_account = inventory, cogs, sales
        inv.shrinkage_account, inv.writedown_account, inv.opening_equity_account = shrink, writedown, opening
        inv.save()
        self.items = [
            Item.objects.create(
                sku=sku, name=sku, item_type=ItemType.STOCK, costing_method=method, purchase_cost=D("1.00"),
                income_account=sales, cogs_account=cogs, inventory_account=inventory)
            for sku, method in (("FIFO", CostingMethod.FIFO), ("AVG", CostingMethod.WEIGHTED_AVERAGE))
        ]
        self.customer = Customer.objects.create(name="One Co")
        self.vendors = [Vendor.objects.create(name="Acme Mfg"), Vendor.objects.create(name="Zed Parts")]
        self.invoice_count = 0

    def pick(self, queryset, n):
        rows = list(queryset)
        return rows[n % len(rows)] if rows else None

    def apply_op(self, op, a, b, day, cents):
        u = self.user
        amount = D(cents) / D(100)
        price = D(cents) / D(1000)
        qty = 1 + cents % 40
        when = d(10, day)
        item = self.items[a % 2]
        vendor = self.vendors[a % 2]
        if op == 0:
            create_po(vendor=vendor, date=when, user=u, lines=[POLineSpec(item, qty, price)])
        elif op == 1:
            po = self.pick(PurchaseOrder.objects.filter(status="OPEN"), a)
            if po:
                quantities = {l.pk: min(po_remaining(l), qty) for l in po.lines.all() if po_remaining(l) > 0}
                bill_from_po(po, date=max(when, po.date), vendor_invoice_number=f"V{cents}", quantities=quantities, user=u,
                             allow_duplicate=True,
                             expense_lines=[BillExpenseSpec(self.freight, amount)] if b % 2 else ())
        elif op == 2:
            expense = [BillExpenseSpec(self.freight, amount)] if b % 2 or not qty % 2 else []
            lines = [BillItemSpec(item, qty, price)] if b % 3 else []
            create_bill(vendor=vendor, date=when, vendor_invoice_number=f"E{cents}", user=u, item_lines=lines,
                        expense_lines=expense, allow_duplicate=True)
        elif op == 3:
            bill = self.pick(Bill.objects.filter(status=DocStatus.OPEN), a)
            if bill:
                items = [BillItemSpec(l.item, l.quantity, l.price + D("0.01"), po_line=l.po_line) for l in bill.item_lines.all()]
                if b % 3 == 0:
                    items = [BillItemSpec(l.item, max(1, l.quantity - 1), l.price, po_line=l.po_line) for l in bill.item_lines.all()]
                expense = [BillExpenseSpec(l.account, l.amount + D("1.00")) for l in bill.expense_lines.all()]
                edit_bill(bill, user=u, date=bill.date, vendor_invoice_number=bill.vendor_invoice_number,
                          item_lines=items, expense_lines=expense, allow_duplicate=True, allow_negative=bool(b % 2))
        elif op == 4:
            bill = self.pick((x for x in Bill.objects.filter(status=DocStatus.OPEN) if bill_balance(x) > 0), a)
            if bill:
                method = [M.ACH, M.CHECK, M.CARD][b % 3]
                paid_from = self.card if method == M.CARD else self.bank
                pay = min(amount, bill_balance(bill))
                extra = D("3.00") if day % 4 == 0 else D("0.00")
                pay_vendor(vendor=bill.vendor, date=max(when, bill.date), method=method, amount=pay + extra,
                           paid_from=paid_from, user=u, check_number=f"C{cents}", applications=[(bill, pay)])
        elif op == 5:
            payment = self.pick((p for p in VendorPayment.objects.filter(status=DocStatus.OPEN) if vendor_payment_unapplied(p) > 0), a)
            bill = self.pick((x for x in Bill.objects.filter(status=DocStatus.OPEN, vendor=payment.vendor)
                              if bill_balance(x) > 0), b) if payment else None
            if payment and bill:
                apply_vendor_payment(payment, bill, min(amount, vendor_payment_unapplied(payment), bill_balance(bill)),
                                     date=max(when, payment.date, bill.date), user=u)
        elif op == 6:
            apps = list(BillPaymentApplication.objects.filter(removed=False)) + list(
                VendorCreditApplication.objects.filter(removed=False))
            if apps:
                app = apps[a % len(apps)]
                remove_bill_application(app, date=max(when, app.applied_on), user=u)
        elif op == 7:
            if b % 2:
                create_return_credit(vendor=vendor, date=when, user=u, reason="r",
                                     lines=[ReturnLineSpec(item, qty, amount if b % 4 == 1 else None)],
                                     allow_negative=bool(day % 2))
            else:
                create_allowance_credit(vendor=vendor, date=when, user=u, reason="r",
                                        lines=[AllowanceLineSpec(self.freight, amount)])
        elif op == 8:
            credit = self.pick((c for c in VendorCredit.objects.filter(status=DocStatus.OPEN) if vendor_credit_unapplied(c) > 0), a)
            bill = self.pick((x for x in Bill.objects.filter(status=DocStatus.OPEN, vendor=credit.vendor)
                              if bill_balance(x) > 0), b) if credit else None
            if credit and bill:
                apply_vendor_credit(credit, bill, min(amount, vendor_credit_unapplied(credit), bill_balance(bill)),
                                    date=max(when, credit.date, bill.date), user=u)
        elif op == 9:
            use_date = when if day % 3 == 0 else None
            kind = b % 3
            if kind == 0:
                doc = self.pick(Bill.objects.filter(status=DocStatus.OPEN), a)
                doc and void_bill(doc, user=u, reason="r", date=use_date, allow_negative=bool(day % 2))
            elif kind == 1:
                doc = self.pick(VendorPayment.objects.filter(status=DocStatus.OPEN), a)
                doc and void_vendor_payment(doc, user=u, reason="r", date=use_date)
            else:
                doc = self.pick(VendorCredit.objects.filter(status=DocStatus.OPEN), a)
                doc and void_vendor_credit(doc, user=u, reason="r", date=use_date, allow_negative=bool(day % 2))
        elif op == 10:
            order = create_order(customer=self.customer, date=when, user=u, lines=[
                OrderLineSpec(item, qty, D("9.00"), fulfillment=Fulfillment.DROP_SHIP,
                              drop_ship_vendor=[" acme  MFG", "Zed Parts"][b % 2])])
            create_drop_ship_pos(order, user=u, date=when)
        elif op == 11:
            order = self.pick(SalesOrder.objects.filter(status="OPEN"), a)
            if order and b % 3:
                line = order.lines.first()
                invoice_from_order(order, date=max(when, order.date), quantities={line.pk: 1 + qty % line.quantity}, user=u)
            else:
                create_invoice(customer=self.customer, date=when, user=u, allow_negative=True,
                               lines=[InvoiceLineSpec(item, 1 + qty % 5, D("3.00"))])
        else:
            doc = self.pick(Invoice.objects.filter(status=DocStatus.OPEN), a)
            doc and void_invoice(doc, user=u, reason="r", date=when if when >= doc.date else None)

    def all_checks(self, as_of=None):
        return ap_check(as_of) + ar_check(as_of) + undeposited_check(as_of)

    @given(ops=st.lists(
        st.tuples(st.integers(0, 12), st.integers(0, 7), st.integers(0, 7), st.integers(1, 28), st.integers(100, 50000)),
        min_size=1, max_size=14))
    @hyp_settings(max_examples=60, deadline=None)
    def test_random_operations_keep_every_check_at_zero(self, ops):
        for op, a, b, day, cents in ops:
            try:
                self.apply_op(op, a, b, day, cents)
            except (PurchasingError, SalesError, PostingError, StockError, PermissionDenied, ValueError):
                continue
            assert self.all_checks() == [] and check_invariants() == [], (op, a, b, day, cents)
        for day in range(1, 31):
            assert self.all_checks(d(10, day)) == [], day

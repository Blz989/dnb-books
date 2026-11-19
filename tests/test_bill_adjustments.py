"""Bill adjustments: posting, edits, refusals, drop-ship shares, and order profit (adjustments spec, Tasks 2-3)."""
from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Vendor
from handling.profit import order_profit
from ledger.models import AccountSubtype, AccountType
from purchasing.balances import bill_balance, bill_total
from purchasing.errors import PurchasingError
from purchasing.models import BillRevision, PurchaseOrder
from purchasing.posting.bills import (
    BillAdjustmentSpec, BillExpenseSpec, BillItemSpec, adjustment_shares, bill_adjustment, bill_from_po, create_bill,
    edit_bill,
)
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from purchasing.models import VendorPaymentMethod
from purchasing.posting.payments import PayLine, pay_bills
from sales.models import Fulfillment
from sales.posting.invoices import invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order
from tests.conftest import _account

D = Decimal
DS = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def wholesale(db):
    return _account("5010", "Wholesale Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)


@pytest.fixture
def setup(handling_setup, inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")
    return purchasing_settings


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")


@pytest.fixture
def part(setup, make_item):
    return make_item("PART", sale_price="20.0000", purchase_cost="7.0000")


def adj(account, amount, **kw):
    return BillAdjustmentSpec(account, D(amount), **kw)


def expense_bill(vendor, user, expense, amount="100.00", adjustment=None):
    return create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="", user=user,
                       expense_lines=[BillExpenseSpec(expense, D(amount), "Freight")], adjustment=adjustment)


def keep_lines(bill):
    """The bill's current lines as specs, for an edit that only changes the adjustment."""
    items = [BillItemSpec(l.item, l.quantity, l.price, l.description, l.po_line)
             for l in bill.item_lines.select_related("item", "po_line")]
    expenses = [BillExpenseSpec(l.account, l.amount, l.description) for l in bill.expense_lines.filter(is_adjustment=False)]
    return items, expenses


def edit(bill, user, adjustment, **kw):
    items, expenses = keep_lines(bill)
    return edit_bill(bill, user=user, date=bill.date, vendor_invoice_number=bill.vendor_invoice_number,
                     item_lines=items, expense_lines=expenses, adjustment=adjustment, **kw)


# ---- posting ----

def test_negative_adjustment_credits_its_account(setup, vendor, admin_user, expense, wholesale):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "-12.50", description="Nova rounding"))
    line = bill_adjustment(bill)
    assert (line.amount, line.account, line.description, line.is_adjustment, line.spreads_to_orders) == (
        D("-12.50"), wholesale, "Nova rounding", True, True)
    assert list(bill.expense_lines.values_list("is_adjustment", flat=True)) == [False, True]  # last
    assert bill_total(bill) == D("87.50") and bill_balance(bill) == D("87.50")
    assert entry_lines(bill.journal_entry) == [
        ("2000", D("0.00"), D("87.50")), ("5010", D("0.00"), D("12.50")), ("6000", D("100.00"), D("0.00"))]


def test_positive_adjustment_debits_its_account(setup, vendor, admin_user, expense, wholesale):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "10.00"))
    assert bill_total(bill) == D("110.00")
    assert bill_adjustment(bill).description == "Adjustment"
    assert entry_lines(bill.journal_entry) == [
        ("2000", D("0.00"), D("110.00")), ("5010", D("10.00"), D("0.00")), ("6000", D("100.00"), D("0.00"))]


def test_adjustment_on_same_account_nets(setup, vendor, admin_user, expense):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(expense, "-1.00"))
    assert entry_lines(bill.journal_entry) == [("2000", D("0.00"), D("99.00")), ("6000", D("99.00"), D("0.00"))]


@pytest.mark.parametrize("amount, message", [
    ("0.00", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("NaN", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("1.005", "Adjustment: amounts can have at most 2 decimal places."),
    ("-100.01", "The bill total can't be below zero."),
])
def test_adjustment_refusals(setup, vendor, admin_user, expense, wholesale, amount, message):
    with pytest.raises(PurchasingError) as exc:
        expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, amount))
    assert str(exc.value) == message


def test_adjustment_refuses_inventory_and_ap(setup, vendor, admin_user, expense, inventory_account, ap):
    for account in (inventory_account, ap):
        with pytest.raises(PurchasingError) as exc:
            expense_bill(vendor, admin_user, expense, adjustment=adj(account, "-1.00"))
        assert str(exc.value) == "Adjustment: choose an account other than inventory or accounts payable."


def test_adjustment_can_bring_total_to_zero(setup, vendor, admin_user, expense, wholesale):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "-100.00"))
    assert bill_total(bill) == D("0.00") and bill.journal_entry is not None


# ---- edits ----

def test_edit_changes_and_removes_the_adjustment(setup, vendor, admin_user, expense, wholesale):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "-62.00"))
    edit(bill, admin_user, adj(wholesale, "-52.00"), reason="Credit was 10.00 too large")
    assert bill_total(bill) == D("48.00") and bill_adjustment(bill).amount == D("-52.00")
    assert bill.expense_lines.filter(is_adjustment=True).count() == 1
    snap = BillRevision.objects.get(bill=bill, revision=1).snapshot
    assert snap["adjustment"]["amount"] == "-62.00" and all("Adjustment" != l["description"] for l in snap["expense_lines"])

    edit(bill, admin_user, None)
    assert bill_adjustment(bill) is None and bill_total(bill) == D("100.00")
    assert entry_lines(bill.journal_entry) == [("2000", D("0.00"), D("100.00")), ("6000", D("100.00"), D("0.00"))]


def test_edit_cannot_drop_total_below_applied(setup, vendor, admin_user, expense, wholesale, bank):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "-10.00"))
    pay_bills(date=d(10, 6), method=VendorPaymentMethod.ACH, paid_from=bank, lines=[PayLine(bill, D("90.00"))],
              user=admin_user)
    with pytest.raises(PurchasingError) as exc:
        edit(bill, admin_user, adj(wholesale, "-10.01"))
    assert str(exc.value) == "This bill has 90.00 applied. The new total can't be less than that."
    edit(bill, admin_user, adj(wholesale, "0.50"))
    assert bill_balance(bill) == D("10.50")


def test_converted_adjustment_keeps_its_flag_when_amount_unchanged(setup, vendor, admin_user, expense, wholesale):
    bill = expense_bill(vendor, admin_user, expense, adjustment=adj(wholesale, "-3.00", spreads_to_orders=False))
    edit(bill, admin_user, adj(wholesale, "-3.00", description="Zoho"))
    assert bill_adjustment(bill).spreads_to_orders is False
    edit(bill, admin_user, adj(wholesale, "-4.00"))
    assert bill_adjustment(bill).spreads_to_orders is True


# ---- shares ----

@pytest.mark.parametrize("amounts, adjustment, shares", [
    (["100.00", "50.00", "50.00"], "-62.00", ["-31.00", "-15.50", "-15.50"]),
    (["10.00", "10.00", "10.00"], "-1.00", ["-0.34", "-0.33", "-0.33"]),
    (["10.00", "20.00"], "0.01", ["0.00", "0.01"]),
    (["0.00", "0.00"], "5.00", ["0.00", "0.00"]),
    ([], "5.00", []),
])
def test_adjustment_shares(amounts, adjustment, shares):
    got = adjustment_shares([D(a) for a in amounts], D(adjustment))
    assert got == [D(s) for s in shares]
    if amounts and any(D(a) for a in amounts):
        assert sum(got) == D(adjustment)


def test_shares_are_stored_on_item_lines_and_zero_when_not_spreading(setup, vendor, admin_user, gadget, part, wholesale):
    po = create_po(vendor=vendor, date=d(10, 1), user=admin_user,
                   lines=[POLineSpec(gadget, 10, D("4.00")), POLineSpec(part, 3, D("7.00"))])
    quantities = {l.pk: l.quantity for l in po.lines.all()}
    bill = bill_from_po(po, date=d(10, 5), vendor_invoice_number="N-1", quantities=quantities, user=admin_user,
                        adjustment=adj(wholesale, "-6.10"))
    assert sorted(bill.item_lines.values_list("amount", "adjustment_share")) == [
        (D("21.00"), D("-2.10")), (D("40.00"), D("-4.00"))]
    edit(bill, admin_user, adj(wholesale, "-6.10", spreads_to_orders=False))
    assert set(bill.item_lines.values_list("adjustment_share", flat=True)) == {D("0.00")}


# ---- drop-ship order cost (Task 3) ----

def drop_ship_order(customer, user, gadget, qty=3):
    so = create_order(customer=customer, date=d(10, 1), user=user, lines=[OrderLineSpec(gadget, qty, D("9.50"), **DS)])
    invoice_from_order(so, date=d(10, 3), quantities={so.lines.get().pk: qty}, user=user)
    return so


def drop_ship_bill(so, user, qty, price, adjustment=None):
    existing = PurchaseOrder.for_sales_order(so)
    (po,) = (existing.get(),) if existing.exists() else create_drop_ship_pos(so, user=user, date=d(10, 2))
    line = po.lines.get()
    return bill_from_po(po, date=d(10, 8), vendor_invoice_number="V-1", quantities={line.pk: qty},
                        prices={line.pk: D(price)}, user=user, adjustment=adjustment)


def test_spreading_adjustment_lowers_drop_ship_cost(setup, customer, admin_user, gadget, wholesale):
    so = drop_ship_order(customer, admin_user, gadget)
    drop_ship_bill(so, admin_user, 3, "4.00", adj(wholesale, "-1.50"))
    p = order_profit(so.number, d(10, 31))
    assert (p.sales, p.cost, p.profit) == (D("28.50"), D("10.50"), D("18.00"))


def test_converted_adjustment_does_not_change_drop_ship_cost(setup, customer, admin_user, gadget, wholesale):
    so = drop_ship_order(customer, admin_user, gadget)
    drop_ship_bill(so, admin_user, 3, "4.00", adj(wholesale, "-1.50", spreads_to_orders=False))
    assert order_profit(so.number, d(10, 31)).cost == D("12.00")


def test_mixed_bill_counts_only_the_drop_ship_share(setup, customer, admin_user, gadget, part, wholesale):
    so = drop_ship_order(customer, admin_user, gadget)
    (po,) = create_drop_ship_pos(so, user=admin_user, date=d(10, 2))
    ds_line = po.lines.get()
    # One bill with the drop-ship line (12.00) and a stock line (36.00): -4.80 splits -1.20 and -3.60.
    bill = create_bill(vendor=po.vendor, date=d(10, 8), vendor_invoice_number="V-9", user=admin_user,
                       purchase_order=po, adjustment=adj(wholesale, "-4.80"),
                       item_lines=[BillItemSpec(gadget, 3, D("4.00"), po_line=ds_line), BillItemSpec(part, 6, D("6.00"))])
    assert sorted(bill.item_lines.values_list("amount", "adjustment_share")) == [
        (D("12.00"), D("-1.20")), (D("36.00"), D("-3.60"))]
    assert order_profit(so.number, d(10, 31)).cost == D("10.80")


def test_editing_the_adjustment_changes_handling_owed(setup, customer, admin_user, gadget, wholesale):
    so = drop_ship_order(customer, admin_user, gadget)
    bill = drop_ship_bill(so, admin_user, 3, "4.00")
    before = order_profit(so.number, d(10, 31))
    edit(bill, admin_user, adj(wholesale, "-3.00"))
    after = order_profit(so.number, d(10, 31))
    assert after.profit - before.profit == D("3.00")
    assert after.owed - before.owed == (D("3.00") * after.rate).quantize(D("0.01"))

from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Terms, Vendor
from items.errors import SettingsMissingError
from items.models import ItemType, MovementKind
from items.stock import current_unit_cost, on_hand
from ledger.balances import account_totals, money
from ledger.errors import PeriodClosedError
from ledger.models import JournalEntry
from ledger.periods import close_period
from purchasing.balances import bill_total
from purchasing.errors import PurchasingError, PurchasingSettingsMissing
from purchasing.posting.bills import (
    BillExpenseSpec, BillItemSpec, DuplicateBillWarning, bill_from_po, create_bill,
)
from purchasing.posting.orders import POLineSpec, create_po
from purchasing.models import Bill, BillLineKind
from sales.models import OrderStatus

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


def ap_balance(ap):
    debit, credit = account_totals(start=None, end=date.max).get(ap.pk, (D("0.00"), D("0.00")))
    return money(credit - debit)


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.5000")


@pytest.fixture
def service(setup, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None,
                     purchase_cost="12.0000")


@pytest.fixture
def po(vendor, admin_user, widget):
    return create_po(vendor=vendor, date=d(10, 1), user=admin_user, lines=[POLineSpec(widget, 1000, D("0.50"))])


def receive(po, admin_user, qty, price=None, number_suffix="1", **kwargs):
    line = po.lines.first()
    return bill_from_po(
        po, date=d(10, 5), vendor_invoice_number=f"INV-{number_suffix}", quantities={line.pk: qty},
        prices={line.pk: price} if price is not None else None, user=admin_user, **kwargs)


def test_stock_bill_raises_stock_at_bill_price(po, widget, admin_user, ap):
    bill = receive(po, admin_user, 500, D("0.48"))
    assert bill.number == "BILL-00001" and bill.status == "OPEN"
    (line,) = bill.item_lines.all()
    assert (line.kind, line.quantity, line.price, line.amount) == (BillLineKind.STOCK, 500, D("0.4800"), D("240.00"))
    assert on_hand(widget) == 500
    assert current_unit_cost(widget) == D("0.4800")
    movement = line.stock_movement
    assert (movement.kind, movement.quantity, movement.total_cost) == (MovementKind.RECEIPT, 500, D("240.00"))
    assert (movement.source_type, movement.source_id) == ("purchasing.bill", "BILL-00001")
    assert entry_lines(movement.journal_entry) == [("1300", D("240.00"), D("0")), ("2000", D("0"), D("240.00"))]
    assert bill.journal_entry is None  # nothing but stock, so the stock entry is the whole posting
    assert ap_balance(ap) == bill_total(bill) == D("240.00")
    assert bill.due_date == d(11, 4) and bill.terms == Terms.NET_30


def test_mixed_bill_posts_expense_line(po, widget, admin_user, freight, ap):
    bill = receive(po, admin_user, 500, D("0.48"), expense_lines=[BillExpenseSpec(freight, D("25.00"), "Freight")])
    assert entry_lines(bill.journal_entry) == [("2000", D("0"), D("25.00")), ("6150", D("25.00"), D("0"))]
    assert (bill.journal_entry.source_type, bill.journal_entry.source_id) == ("purchasing.bill", "BILL-00001")
    assert bill_total(bill) == D("265.00")
    assert ap_balance(ap) == D("265.00")


def test_odd_prices_still_tie_to_the_ledger(vendor, admin_user, widget, freight, ap):
    bill = create_bill(
        vendor=vendor, date=d(10, 5), vendor_invoice_number="ODD", user=admin_user,
        item_lines=[BillItemSpec(widget, 3, D("0.3333")), BillItemSpec(widget, 7, D("0.1250"))],
        expense_lines=[BillExpenseSpec(freight, D("1.10"))])
    assert bill_total(bill) == D("2.98")  # 1.00 + 0.88 + 1.10
    assert ap_balance(ap) == bill_total(bill)


def test_partial_receiving_and_auto_close(po, admin_user):
    receive(po, admin_user, 400)
    po.refresh_from_db()
    assert po.status == OrderStatus.OPEN
    receive(po, admin_user, 600, number_suffix="2")
    po.refresh_from_db()
    assert po.status == OrderStatus.CLOSED


def test_over_receiving_refused(po, admin_user):
    receive(po, admin_user, 500)
    with pytest.raises(PurchasingError, match=r"Line 1: only 500 left to receive on PO-00001\."):
        receive(po, admin_user, 501, number_suffix="2")
    assert Bill.objects.count() == 1


def test_same_po_line_twice_in_one_bill_is_counted_together(po, vendor, admin_user, widget):
    line = po.lines.get()
    with pytest.raises(PurchasingError, match=r"Line 2: only 100 left to receive on PO-00001\."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user, purchase_order=po,
                    item_lines=[BillItemSpec(widget, 900, D("0.50"), po_line=line),
                                BillItemSpec(widget, 200, D("0.50"), po_line=line)])


def test_duplicate_vendor_invoice_number_warns(po, vendor, admin_user, widget):
    create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="INV-88123", user=admin_user,
                item_lines=[BillItemSpec(widget, 1, D("1.00"))])
    with pytest.raises(DuplicateBillWarning,
                       match=r"Acme Supply already has bill BILL-00001 with invoice # INV-88123\. Save anyway\?"):
        create_bill(vendor=vendor, date=d(10, 6), vendor_invoice_number="inv-88123 ", user=admin_user,
                    item_lines=[BillItemSpec(widget, 1, D("1.00"))])
    assert Bill.objects.count() == 1
    again = create_bill(vendor=vendor, date=d(10, 6), vendor_invoice_number="inv-88123 ", user=admin_user,
                        item_lines=[BillItemSpec(widget, 1, D("1.00"))], allow_duplicate=True)
    assert again.number == "BILL-00002"


def test_duplicate_check_ignores_other_vendors_and_void_bills(vendor, admin_user, widget):
    other = Vendor.objects.create(name="Other Co")
    first = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="A1", user=admin_user,
                        item_lines=[BillItemSpec(widget, 1, D("1.00"))])
    create_bill(vendor=other, date=d(10, 5), vendor_invoice_number="A1", user=admin_user,
                item_lines=[BillItemSpec(widget, 1, D("1.00"))])
    first.status, first.voided_on = "VOID", d(10, 6)
    first.save()
    create_bill(vendor=vendor, date=d(10, 7), vendor_invoice_number="a1", user=admin_user,
                item_lines=[BillItemSpec(widget, 1, D("1.00"))])


def test_stock_line_needs_inventory_settings(vendor, admin_user, make_item, purchasing_settings, ap):
    item = make_item("RAW")
    with pytest.raises(SettingsMissingError, match="Set up inventory accounts first."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user,
                    item_lines=[BillItemSpec(item, 5, D("2.00"))])
    assert Bill.objects.count() == 0 and JournalEntry.objects.count() == 0


def test_expense_only_bill_without_inventory_settings(vendor, admin_user, expense, purchasing_settings, ap):
    bill = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="RENT", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("1500.00"), "October rent")])
    assert entry_lines(bill.journal_entry) == [("2000", D("0"), D("1500.00")), ("6000", D("1500.00"), D("0"))]
    assert ap_balance(ap) == D("1500.00")


def test_non_stock_item_line_uses_vendor_default_account_else_cogs_default(vendor, admin_user, service, expense):
    first = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="S1", user=admin_user,
                        item_lines=[BillItemSpec(service, 2, D("12.00"))])
    assert entry_lines(first.journal_entry) == [("2000", D("0"), D("24.00")), ("5000", D("24.00"), D("0"))]
    vendor.default_expense_account = expense
    vendor.save()
    second = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="S2", user=admin_user,
                         item_lines=[BillItemSpec(service, 1, D("12.00"))])
    assert entry_lines(second.journal_entry) == [("2000", D("0"), D("12.00")), ("6000", D("12.00"), D("0"))]
    assert second.item_lines.get().kind == BillLineKind.NON_STOCK


def test_missing_default_account_names_the_line(vendor, admin_user, service, inv_settings):
    inv_settings.default_cogs_account = None
    inv_settings.save()
    with pytest.raises(PurchasingError, match="Line 1: choose an account for SVC."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="S1", user=admin_user,
                    item_lines=[BillItemSpec(service, 1, D("12.00"))])


def test_validation_messages(vendor, admin_user, widget, po):
    with pytest.raises(PurchasingError, match="A bill needs at least one line."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user)
    other = Vendor.objects.create(name="Other Co")
    with pytest.raises(PurchasingError, match="PO-00001 is for a different vendor."):
        create_bill(vendor=other, date=d(10, 5), vendor_invoice_number="X", user=admin_user, purchase_order=po,
                    item_lines=[BillItemSpec(widget, 1, D("1.00"), po_line=po.lines.get())])
    vendor.is_active = False
    vendor.save()
    with pytest.raises(PurchasingError, match="Acme Supply is inactive."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user,
                    item_lines=[BillItemSpec(widget, 1, D("1.00"))])


def test_settings_missing(vendor, admin_user, expense, ap):
    with pytest.raises(PurchasingSettingsMissing, match="Set up purchasing accounts first."):
        create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user,
                    expense_lines=[BillExpenseSpec(expense, D("5.00"))])


def test_closed_month_refuses(vendor, admin_user, expense, purchasing_settings):
    close_period(2026, 9, user=admin_user)
    with pytest.raises(PeriodClosedError):
        create_bill(vendor=vendor, date=d(9, 15), vendor_invoice_number="X", user=admin_user,
                    expense_lines=[BillExpenseSpec(expense, D("5.00"))])
    assert Bill.objects.count() == 0


def test_bill_dates_default_terms_from_vendor(vendor, admin_user, expense, purchasing_settings):
    bill = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="X", user=admin_user, terms=Terms.NET_15,
                       expense_lines=[BillExpenseSpec(expense, D("5.00"))])
    assert (bill.terms, bill.due_date) == (Terms.NET_15, d(10, 20))


def test_zero_total_bill_posts_no_entry(vendor, admin_user, service):
    bill = create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="FREE", user=admin_user,
                       item_lines=[BillItemSpec(service, 1, D("0.00"))])
    assert bill.journal_entry is None

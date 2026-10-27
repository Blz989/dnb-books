from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer, Terms
from items.errors import NegativeStockWarning, SettingsMissingError
from items.models import ItemType, MovementKind, StockMovement
from items.stock import StockLine, on_hand, record_movements
from ledger.errors import PeriodClosedError
from ledger.models import JournalEntry
from ledger.periods import close_period
from sales.errors import InvoiceError, SalesSettingsMissing
from sales.models import DocStatus, Fulfillment, Invoice, OrderStatus
from sales.posting.invoices import InvoiceLineSpec, create_invoice, invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def setup(inv_settings, sales_settings):
    return sales_settings


@pytest.fixture
def bolt(setup, admin_user, make_item):
    item = make_item("BOLT", sale_price="0.8500")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user,
                     lines=[StockLine(item, 10, unit_cost=D("0.50"))])
    return item


@pytest.fixture
def service(setup, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


def test_invoice_posts_revenue_and_cogs(bolt, customer, admin_user):
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(bolt, 6, D("0.85"))])
    assert inv.number == "INV-000001" and inv.revision == 1 and inv.status == DocStatus.OPEN
    (line,) = inv.lines.all()
    assert line.amount == D("5.10")
    assert entry_lines(inv.journal_entry) == [("1200", D("5.10"), D("0")), ("4000", D("0"), D("5.10"))]
    assert (inv.journal_entry.source_type, inv.journal_entry.source_id) == ("sales.invoice", "INV-000001")
    movement = line.stock_movement
    assert (movement.kind, movement.quantity, movement.total_cost) == (MovementKind.SALE, -6, D("-3.00"))
    assert (movement.source_type, movement.source_id) == ("sales.invoice", "INV-000001")
    assert entry_lines(movement.journal_entry) == [("1300", D("0"), D("3.00")), ("5000", D("3.00"), D("0"))]
    assert on_hand(bolt) == 4


def test_line_amount_rounds_half_up(bolt, service, customer, admin_user):
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                         lines=[InvoiceLineSpec(service, 3, D("0.8350"))])
    assert inv.lines.get().amount == D("2.51")  # 2.505 rounds up


def test_revenue_is_grouped_by_income_account(bolt, service, customer, admin_user, db):
    from tests.conftest import _account
    from ledger.models import AccountSubtype, AccountType

    fees = _account("4100", "Service income", AccountType.INCOME, AccountSubtype.INCOME)
    svc2 = service.__class__.objects.create(
        sku="SVC2", name="Svc 2", item_type=ItemType.NON_STOCK, income_account=fees, sale_price=D("10"))
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
        InvoiceLineSpec(bolt, 2, D("1.00")), InvoiceLineSpec(service, 1, D("5.00")),
        InvoiceLineSpec(svc2, 1, D("7.00"))])
    assert entry_lines(inv.journal_entry) == [
        ("1200", D("14.00"), D("0")), ("4000", D("0"), D("7.00")), ("4100", D("0"), D("7.00"))]


def test_partial_shipments_and_auto_close(bolt, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 10, D("0.85"))])
    (line,) = order.lines.all()
    record_movements(kind=MovementKind.OPENING, date=d(10, 2), user=admin_user,
                     lines=[StockLine(bolt, 10, unit_cost=D("0.50"))])
    first = invoice_from_order(order, date=d(10, 5), quantities={line.pk: 6}, user=admin_user)
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN and first.sales_order == order
    second = invoice_from_order(order, date=d(10, 9), quantities={line.pk: 4}, user=admin_user)
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED
    assert (first.number, second.number) == ("INV-000001", "INV-000002")
    assert first.lines.get().so_line == line and first.lines.get().price == D("0.8500")


def test_from_order_skips_zero_lines_and_uses_order_values(bolt, service, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, ship_to="Job site", lines=[
        OrderLineSpec(bolt, 5, D("0.85"), description="Hex bolt"),
        OrderLineSpec(service, 2, D("40.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])
    stock_line, drop_line = order.lines.all()
    inv = invoice_from_order(order, date=d(10, 5), quantities={stock_line.pk: 0, drop_line.pk: 2}, user=admin_user)
    (only,) = inv.lines.all()
    assert (only.item, only.quantity, only.fulfillment, only.drop_ship_vendor) == (service, 2, Fulfillment.DROP_SHIP, "Acme Mfg")
    assert inv.ship_to == "Job site"


def test_over_invoicing_refused(bolt, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 10, D("0.85"))])
    (line,) = order.lines.all()
    invoice_from_order(order, date=d(10, 5), quantities={line.pk: 6}, user=admin_user)
    with pytest.raises(InvoiceError, match=r"Line 1: only 4 left to invoice on SO-00001\."):
        invoice_from_order(order, date=d(10, 6), quantities={line.pk: 5}, user=admin_user)
    assert Invoice.objects.count() == 1


def test_over_invoicing_counts_the_same_order_line_twice_in_one_invoice(bolt, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 10, D("0.85"))])
    (line,) = order.lines.all()
    with pytest.raises(InvoiceError, match=r"Line 2: only 4 left to invoice on SO-00001\."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, sales_order=order, lines=[
            InvoiceLineSpec(bolt, 6, D("0.85"), so_line=line), InvoiceLineSpec(bolt, 5, D("0.85"), so_line=line)])


def test_order_must_belong_to_the_customer(bolt, customer, admin_user):
    other = Customer.objects.create(name="Other Co")
    order = create_order(customer=other, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 1, D("1.00"))])
    (line,) = order.lines.all()
    with pytest.raises(InvoiceError, match="SO-00001 belongs to a different customer."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, sales_order=order,
                       lines=[InvoiceLineSpec(bolt, 1, D("1.00"), so_line=line)])


def test_closed_order_cannot_be_invoiced(bolt, customer, admin_user):
    from sales.posting.orders import close_order

    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 3, D("1.00"))])
    (line,) = order.lines.all()
    close_order(order, admin_user)
    with pytest.raises(InvoiceError, match="SO-00001 is closed. Reopen it to invoice it."):
        invoice_from_order(order, date=d(10, 5), quantities={line.pk: 1}, user=admin_user)


def test_drop_ship_line_moves_no_stock(setup, customer, admin_user, make_item):
    item = make_item("DS-1")  # stock item, nothing on hand
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
        InvoiceLineSpec(item, 3, D("20.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")])
    (line,) = inv.lines.all()
    assert line.stock_movement is None and not line.drop_ship_billed
    assert not StockMovement.objects.filter(source_id=inv.number).exists()
    assert entry_lines(inv.journal_entry) == [("1200", D("60.00"), D("0")), ("4000", D("0"), D("60.00"))]


def test_non_stock_item_with_stock_fulfillment_moves_nothing(service, customer, admin_user):
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("50.00"))])
    assert inv.lines.get().stock_movement is None


def test_negative_stock_warns_then_saves(bolt, customer, admin_user):
    lines = [InvoiceLineSpec(bolt, 12, D("0.85"))]
    with pytest.raises(NegativeStockWarning) as caught:
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=lines)
    assert "Line 1: BOLT has 10 on hand. Saving will take it to -2." in caught.value.messages[0]
    assert not Invoice.objects.exists() and not JournalEntry.objects.filter(source_type="sales.invoice").exists()
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=lines, allow_negative=True)
    assert inv.number == "INV-000001" and on_hand(bolt) == -2


def test_negative_warning_uses_invoice_line_numbers(bolt, service, customer, admin_user):
    with pytest.raises(NegativeStockWarning) as caught:
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
            InvoiceLineSpec(service, 1, D("5.00")), InvoiceLineSpec(bolt, 12, D("0.85"))])
    assert caught.value.messages[0].startswith("Line 2: BOLT")


def test_inactive_item_plain_message(bolt, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 5, D("0.85"))])
    (line,) = order.lines.all()
    bolt.is_active = False
    bolt.save()
    with pytest.raises(InvoiceError, match=r"^Line 1: BOLT is not an active stock item\.$"):
        invoice_from_order(order, date=d(10, 5), quantities={line.pk: 5}, user=admin_user)
    assert not Invoice.objects.exists() and on_hand(bolt) == 10


def test_drop_ship_only_invoice_without_inventory_settings(sales_settings, customer, admin_user, make_item):
    item = make_item("DS-1")
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
        InvoiceLineSpec(item, 1, D("20.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme")])
    assert inv.journal_entry is not None


def test_stock_line_without_inventory_settings(sales_settings, customer, admin_user, make_item):
    item = make_item("BOLT")
    with pytest.raises(SettingsMissingError, match="Set up inventory accounts first."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(item, 1, D("1.00"))])
    assert not Invoice.objects.exists()


def test_sales_settings_missing_blocks_every_invoice(inv_settings, customer, admin_user, make_item):
    item = make_item("BOLT")
    with pytest.raises(SalesSettingsMissing, match="Set up sales accounts first."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[
            InvoiceLineSpec(item, 1, D("1.00"), fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="x")])


def test_zero_total_invoice_posts_nothing(service, customer, admin_user):
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("0.00"))])
    assert inv.journal_entry is None
    assert not JournalEntry.objects.filter(source_type="sales.invoice").exists()


def test_due_date_from_terms(service, customer, admin_user):
    default = create_invoice(customer=customer, date=d(10, 27), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    assert (default.terms, default.due_date) == (Terms.NET_30, d(11, 26))
    receipt = create_invoice(customer=customer, date=d(10, 27), user=admin_user, terms=Terms.DUE_ON_RECEIPT,
                             lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    assert receipt.due_date == d(10, 27)


def test_defaults_for_ship_to_and_notes(service, sales_settings, customer, admin_user):
    sales_settings.default_invoice_notes = "Thank you!"
    sales_settings.save()
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    assert inv.ship_to == customer.ship_to_address and inv.notes == "Thank you!"
    inv2 = create_invoice(customer=customer, date=d(10, 5), user=admin_user, ship_to="Elsewhere", notes="",
                          lines=[InvoiceLineSpec(service, 1, D("5.00"))])
    assert inv2.ship_to == "Elsewhere" and inv2.notes == ""


def test_validation_messages(service, customer, admin_user):
    with pytest.raises(InvoiceError, match="An invoice needs at least one line with a quantity."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[])
    with pytest.raises(InvoiceError, match="Line 1: price can't be negative."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("-1.00"))])
    with pytest.raises(InvoiceError, match="Line 1: enter a quantity above zero."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 0, D("1.00"))])
    customer.is_active = False
    customer.save()
    with pytest.raises(InvoiceError, match="Acme Builders is inactive."):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("1.00"))])


def test_closed_month_refused_even_for_zero_total(service, customer, admin_user):
    close_period(2026, 9, admin_user)
    with pytest.raises(PeriodClosedError, match="September 2026 is closed"):
        create_invoice(customer=customer, date=d(9, 30), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("0.00"))])
    assert not Invoice.objects.exists()


def test_failed_invoice_gives_the_number_back(service, customer, admin_user):
    with pytest.raises(InvoiceError):
        create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 0, D("1.00"))])
    inv = create_invoice(customer=customer, date=d(10, 5), user=admin_user, lines=[InvoiceLineSpec(service, 1, D("1.00"))])
    assert inv.number == "INV-000001"

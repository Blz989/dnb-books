"""Invoice adjustments: posting, edits, refusals, the ADJ item, order profit (adjustments spec, Task 5)."""
from datetime import date
from decimal import Decimal

import pytest

from handling.profit import order_profit
from items.models import InventorySettings, Item, ItemType
from items.stock import StockLine, record_movements
from items.models import MovementKind
from ledger.models import AccountSubtype, AccountType
from sales.balances import invoice_balance, invoice_total
from sales.errors import InvoiceError
from sales.models import InvoiceRevision, PaymentMethod
from sales.posting.invoices import (
    InvoiceAdjustmentSpec, InvoiceLineSpec, adjustment_item, create_invoice, edit_invoice, invoice_adjustment,
    invoice_from_order,
)
from sales.posting.orders import OrderLineSpec, create_order
from tests.conftest import _account

D = Decimal


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


NON_STOCK = dict(item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def setup(handling_setup, inv_settings, sales_settings):
    return sales_settings


@pytest.fixture
def svc(setup, make_item):
    return make_item("SVC", **NON_STOCK)


@pytest.fixture
def bolt(setup, admin_user, make_item):
    item = make_item("BOLT", sale_price="0.8500")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user,
                     lines=[StockLine(item, 10, unit_cost=D("0.50"))])
    return item


def adj(amount, **kw):
    return InvoiceAdjustmentSpec(D(amount), **kw)


def new_invoice(customer, user, item, price="50.00", adjustment=None):
    return create_invoice(customer=customer, date=d(10, 5), user=user, lines=[InvoiceLineSpec(item, 1, D(price))],
                          adjustment=adjustment)


def edit(inv, user, adjustment, **kw):
    lines = [InvoiceLineSpec(l.item, l.quantity, l.price, l.description) for l in inv.lines.filter(is_adjustment=False)]
    return edit_invoice(inv, user=user, date=inv.date, lines=lines, terms=None, ship_to=inv.ship_to, notes=inv.notes,
                        adjustment=adjustment, **kw)


def test_adjustment_item_created_on_first_use(svc, customer, admin_user, revenue):
    assert not Item.objects.filter(sku="ADJ").exists()
    inv = new_invoice(customer, admin_user, svc, adjustment=adj("-0.40"))
    line = invoice_adjustment(inv)
    assert (line.item.sku, line.item.name, line.item.item_type, line.item.income_account) == (
        "ADJ", "Adjustment", ItemType.NON_STOCK, revenue)
    assert (line.quantity, line.price, line.amount, line.description) == (1, D("-0.4000"), D("-0.40"), "Adjustment")
    assert list(inv.lines.values_list("is_adjustment", flat=True)) == [False, True]
    assert invoice_total(inv) == D("49.60") and invoice_balance(inv) == D("49.60")
    assert entry_lines(inv.journal_entry) == [("1200", D("49.60"), D("0.00")), ("4000", D("0.00"), D("49.60"))]


def test_adjustment_posts_to_the_adj_items_account(svc, customer, admin_user, revenue):
    wholesale = _account("4050", "Wholesale Sales", AccountType.INCOME, AccountSubtype.INCOME)
    item = adjustment_item()
    item.income_account = wholesale
    item.save()
    inv = new_invoice(customer, admin_user, svc, adjustment=adj("5.00", description="Rush fee"))
    assert invoice_adjustment(inv).description == "Rush fee"
    assert entry_lines(inv.journal_entry) == [
        ("1200", D("55.00"), D("0.00")), ("4000", D("0.00"), D("50.00")), ("4050", D("0.00"), D("5.00"))]


def test_no_default_income_account(svc, customer, admin_user):
    settings = InventorySettings.load()
    settings.default_income_account = None
    settings.save()
    with pytest.raises(InvoiceError) as exc:
        new_invoice(customer, admin_user, svc, adjustment=adj("1.00"))
    assert str(exc.value) == "Set the default income account in Settings, Inventory first."


@pytest.mark.parametrize("amount, message", [
    ("0.00", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("Infinity", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("0.001", "Adjustment: amounts can have at most 2 decimal places."),
    ("-50.01", "The invoice total can't be below zero."),
])
def test_refusals(svc, customer, admin_user, amount, message):
    with pytest.raises(InvoiceError) as exc:
        new_invoice(customer, admin_user, svc, adjustment=adj(amount))
    assert str(exc.value) == message


def test_from_order_with_adjustment(bolt, customer, admin_user):
    so = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 10, D("0.85"))])
    (line,) = so.lines.all()
    inv = invoice_from_order(so, date=d(10, 5), quantities={line.pk: 10}, user=admin_user, adjustment=adj("-0.50"))
    assert invoice_total(inv) == D("8.00")
    so.refresh_from_db()
    assert so.status == "CLOSED"
    p = order_profit(so.number, d(10, 31))
    assert (p.sales, p.cost, p.profit) == (D("8.00"), D("5.00"), D("3.00"))


def test_edit_changes_and_removes(svc, customer, admin_user):
    inv = new_invoice(customer, admin_user, svc, adjustment=adj("-2.00"))
    edit(inv, admin_user, adj("3.00"), reason="Rounding")
    assert invoice_total(inv) == D("53.00") and inv.lines.filter(is_adjustment=True).count() == 1
    snap = InvoiceRevision.objects.get(invoice=inv, revision=1).snapshot
    assert snap["adjustment"]["amount"] == "-2.00" and len(snap["lines"]) == 1
    edit(inv, admin_user, None)
    assert invoice_adjustment(inv) is None and invoice_total(inv) == D("50.00")


def test_edit_keeps_an_imported_adjustment_item(svc, customer, admin_user, make_item):
    wholesale = _account("4050", "Wholesale Sales", AccountType.INCOME, AccountSubtype.INCOME)
    copy = make_item("ADJ-4050", income_account=wholesale, **NON_STOCK)
    inv = new_invoice(customer, admin_user, svc, adjustment=adj("-1.00", item=copy))
    edit(inv, admin_user, adj("-1.50"))
    assert invoice_adjustment(inv).item == copy
    assert ("4050", D("1.50"), D("0.00")) in entry_lines(inv.journal_entry)


def test_edit_cannot_drop_total_below_applied(svc, customer, admin_user):
    from sales.posting.receipts import receive_payment

    inv = new_invoice(customer, admin_user, svc)
    receive_payment(customer=customer, date=d(10, 6), amount=D("50.00"), method=PaymentMethod.CHECK, user=admin_user,
                    applications=[(inv, D("50.00"))])
    with pytest.raises(InvoiceError) as exc:
        edit(inv, admin_user, adj("-0.01"))
    assert str(exc.value) == "This invoice has 50.00 applied. The new total can't be less than that."

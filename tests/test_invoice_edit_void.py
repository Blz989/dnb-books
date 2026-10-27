from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from contacts.models import Customer, Terms
from items.errors import NegativeStockWarning
from items.models import MovementKind
from items.stock import StockLine, on_hand, record_movements
from ledger.balances import account_totals
from ledger.errors import PeriodClosedError
from ledger.models import EntryStatus
from ledger.periods import close_period
from sales.errors import InvoiceError
from sales.models import (
    DocStatus, Invoice, InvoiceRevision, OrderStatus, Payment, PaymentApplication, PaymentMethod,
)
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order

D = Decimal


def d(month, day):
    return date(2026, month, day)


def net(account):
    debit, credit = account_totals(start=None, end=d(12, 31)).get(account.pk, (D("0"), D("0")))
    return debit - credit


@pytest.fixture
def bolt(inv_settings, sales_settings, admin_user, make_item):
    item = make_item("BOLT", sale_price="0.8500")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user,
                     lines=[StockLine(item, 10, unit_cost=D("0.50"))])
    return item


@pytest.fixture
def invoice(bolt, customer, admin_user):
    return create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                          lines=[InvoiceLineSpec(bolt, 6, D("0.85"))], notes="Original")


def edit(invoice, user, lines, **kw):
    args = dict(user=user, date=invoice.date, terms=invoice.terms, ship_to=invoice.ship_to, notes=invoice.notes, lines=lines)
    args.update(kw)
    return edit_invoice(invoice, **args)


def apply_payment(invoice, amount, admin_user, sales_settings):
    pay = Payment.objects.create(
        number="PMT-00001", customer=invoice.customer, date=d(10, 6), method=PaymentMethod.CHECK, amount=D(amount),
        deposited_to=sales_settings.undeposited_account, created_by=admin_user)
    return PaymentApplication.objects.create(payment=pay, invoice=invoice, amount=D(amount), applied_on=d(10, 6))


def test_edit_keeps_number_and_snapshot(invoice, bolt, admin_user):
    old_line = invoice.lines.get()
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 3, D("1.00"))], notes="Changed", reason="Customer took fewer")
    assert out.number == "INV-000001" and out.revision == 2 and out.notes == "Changed"
    (rev,) = InvoiceRevision.objects.filter(invoice=invoice)
    assert (rev.revision, rev.edited_by, rev.reason) == (1, admin_user, "Customer took fewer")
    snap = rev.snapshot
    assert snap["notes"] == "Original" and snap["date"] == "2026-10-05" and snap["terms"] == Terms.NET_30
    assert snap["customer_id"] == invoice.customer_id and snap["due_date"] == "2026-11-04"
    assert snap["lines"] == [{
        "item_id": bolt.pk, "sku": "BOLT", "description": "", "quantity": 6, "price": "0.8500", "amount": "5.10",
        "fulfillment": "STOCK", "drop_ship_vendor": "", "so_line_id": None}]
    assert [l.quantity for l in out.lines.all()] == [3] and old_line.pk not in [l.pk for l in out.lines.all()]


def test_edit_reposts_ledger_and_stock(invoice, bolt, admin_user, ar, revenue, cogs_account, inventory_account):
    old_entry, old_movement = invoice.journal_entry, invoice.lines.get().stock_movement
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 3, D("0.85"))])
    old_entry.refresh_from_db()
    old_movement.refresh_from_db()
    assert old_entry.status == EntryStatus.VOID and old_movement.voided
    assert out.journal_entry != old_entry and out.journal_entry.status == EntryStatus.POSTED
    assert on_hand(bolt) == 7
    assert net(ar) == D("2.55") and net(revenue) == D("-2.55")
    assert net(cogs_account) == D("1.50") and net(inventory_account) == D("3.50")


def test_edit_can_change_the_date(invoice, bolt, admin_user):
    old_entry = invoice.journal_entry
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 6, D("0.85"))], date=d(10, 9))
    assert out.date == d(10, 9) and out.due_date == d(11, 8) and out.journal_entry.date == d(10, 9)
    assert old_entry.reversals.get().date == d(10, 5)


def test_edit_into_zero_total_posts_nothing(invoice, bolt, admin_user):
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 1, D("0.00"))])
    assert out.journal_entry is None


def test_edit_from_order_counts_own_quantity(bolt, customer, admin_user):
    record_movements(kind=MovementKind.OPENING, date=d(10, 2), user=admin_user,
                     lines=[StockLine(bolt, 10, unit_cost=D("0.50"))])
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 10, D("0.85"))])
    (so_line,) = order.lines.all()
    a = invoice_from_order(order, date=d(10, 5), quantities={so_line.pk: 6}, user=admin_user)
    invoice_from_order(order, date=d(10, 6), quantities={so_line.pk: 4}, user=admin_user)
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED
    a = edit(a, admin_user, [InvoiceLineSpec(bolt, 6, D("0.85"), so_line=so_line)])
    with pytest.raises(InvoiceError, match=r"Line 1: only 6 left to invoice on SO-00001\."):
        edit(a, admin_user, [InvoiceLineSpec(bolt, 7, D("0.85"), so_line=so_line)])
    a = edit(a, admin_user, [InvoiceLineSpec(bolt, 5, D("0.85"), so_line=so_line)])
    order.refresh_from_db()
    assert a.revision == 3 and order.status == OrderStatus.OPEN


def test_edit_below_applied_refused(invoice, bolt, admin_user, sales_settings):
    apply_payment(invoice, "5.10", admin_user, sales_settings)
    with pytest.raises(InvoiceError, match=r"This invoice has 5\.10 applied\. The new total can't be less than that\."):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 3, D("0.85"))])
    invoice.refresh_from_db()
    assert invoice.revision == 1 and not InvoiceRevision.objects.exists()


def test_customer_cannot_change_after_application(invoice, bolt, admin_user, sales_settings):
    other = Customer.objects.create(name="Other Co")
    app = apply_payment(invoice, "1.00", admin_user, sales_settings)
    with pytest.raises(InvoiceError, match="The customer can't change after payments or credits are applied."):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 6, D("0.85"))], customer=other)
    app.removed, app.removed_on = True, d(10, 7)
    app.save()
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 6, D("0.85"))], customer=other)
    assert out.customer == other


def test_edit_in_closed_month_refused(invoice, bolt, admin_user):
    close_period(2026, 10, admin_user)
    with pytest.raises(PeriodClosedError, match="October 2026 is closed"):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 3, D("0.85"))])


def test_edit_new_date_in_closed_month_refused(invoice, bolt, admin_user):
    close_period(2026, 9, admin_user)
    with pytest.raises(PeriodClosedError, match="September 2026 is closed"):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 3, D("0.85"))], date=d(9, 30))
    invoice.refresh_from_db()
    assert invoice.revision == 1


def test_failed_edit_leaves_everything_as_it_was(invoice, bolt, admin_user):
    with pytest.raises(NegativeStockWarning):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 14, D("0.85"))])
    invoice.refresh_from_db()
    assert invoice.revision == 1 and invoice.journal_entry.status == EntryStatus.POSTED
    assert on_hand(bolt) == 4 and not InvoiceRevision.objects.exists()
    out = edit(invoice, admin_user, [InvoiceLineSpec(bolt, 14, D("0.85"))], allow_negative=True)
    assert on_hand(bolt) == -4 and out.revision == 2


def test_standard_user_can_edit(standard_user, invoice, bolt):
    out = edit(invoice, standard_user, [InvoiceLineSpec(bolt, 2, D("0.85"))])
    assert out.revision == 2 and on_hand(bolt) == 8


def test_void_invoice_cannot_be_edited(invoice, bolt, admin_user):
    void_invoice(invoice, user=admin_user, reason="Mistake")
    with pytest.raises(InvoiceError, match="INV-000001 is void and can't be edited."):
        edit(invoice, admin_user, [InvoiceLineSpec(bolt, 2, D("0.85"))])


def test_void_blocked_with_applications_then_allowed(invoice, bolt, admin_user, sales_settings, ar, revenue):
    app = apply_payment(invoice, "5.10", admin_user, sales_settings)
    with pytest.raises(InvoiceError, match="INV-000001 has payments or credits applied. Remove them first."):
        void_invoice(invoice, user=admin_user, reason="Mistake")
    app.removed, app.removed_on = True, d(10, 8)
    app.save()
    out = void_invoice(invoice, user=admin_user, reason="Mistake", date=d(10, 9))
    assert out.status == DocStatus.VOID and out.voided_on == d(10, 9) and out.void_reason == "Mistake"
    assert out.number == "INV-000001"
    assert on_hand(bolt) == 10 and net(ar) == D("0.00") and net(revenue) == D("0.00")
    assert out.lines.get().stock_movement.voided_on == d(10, 9)


def test_void_needs_admin_and_reason(invoice, standard_user, admin_user):
    with pytest.raises(PermissionDenied):
        void_invoice(invoice, user=standard_user, reason="x")
    with pytest.raises(InvoiceError, match="Enter a reason."):
        void_invoice(invoice, user=admin_user, reason=" ")


def test_void_in_closed_month_refused(invoice, admin_user):
    close_period(2026, 10, admin_user)
    with pytest.raises(PeriodClosedError):
        void_invoice(invoice, user=admin_user, reason="Mistake")
    invoice.refresh_from_db()
    assert invoice.status == DocStatus.OPEN


def test_void_twice_refused(invoice, admin_user):
    void_invoice(invoice, user=admin_user, reason="Mistake")
    with pytest.raises(InvoiceError, match="INV-000001 is already void."):
        void_invoice(invoice, user=admin_user, reason="Again")


def test_void_reopens_closed_order(bolt, customer, admin_user):
    order = create_order(customer=customer, date=d(10, 2), user=admin_user, lines=[OrderLineSpec(bolt, 4, D("0.85"))])
    (so_line,) = order.lines.all()
    inv = invoice_from_order(order, date=d(10, 5), quantities={so_line.pk: 4}, user=admin_user)
    order.refresh_from_db()
    assert order.status == OrderStatus.CLOSED
    void_invoice(inv, user=admin_user, reason="Wrong customer")
    order.refresh_from_db()
    assert order.status == OrderStatus.OPEN


def test_void_date_cannot_precede_invoice_date(invoice, admin_user):
    with pytest.raises(InvoiceError, match="The void date can't be before the invoice date."):
        void_invoice(invoice, user=admin_user, reason="Mistake", date=d(10, 4))

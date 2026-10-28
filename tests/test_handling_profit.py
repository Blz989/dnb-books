from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from contacts.models import Customer, Terms, Vendor
from handling.models import HandlingRun, HandlingRunLine, RunStatus
from handling.profit import billed_to_date, order_profit, orders
from handling.rates import add_rate
from purchasing.posting.bills import bill_from_po
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import Fulfillment
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, invoice_from_order, void_invoice
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note, remove_application

D = Decimal
DS = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def setup(handling_setup, inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")


@pytest.fixture
def gadget(setup, make_item):
    return make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")


@pytest.fixture
def part(setup, make_item):
    return make_item("PART", sale_price="20.0000", purchase_cost="7.0000")


def receive(item, qty, price, day, vendor, user):
    po = create_po(vendor=vendor, date=day, user=user, lines=[POLineSpec(item, qty, D(price))])
    line = po.lines.get()
    return bill_from_po(po, date=day, vendor_invoice_number=f"R-{po.number}", quantities={line.pk: qty}, user=user)


def order_of(customer, user, day, *lines):
    return create_order(customer=customer, date=day, user=user, lines=list(lines))


def invoice(order, user, day, *qtys):
    lines = list(order.lines.all())
    return invoice_from_order(order, date=day, quantities={l.pk: q for l, q in zip(lines, qtys)}, user=user)


def drop_ship_bill(order, user, day, qty, price, suffix="1"):
    (po,) = create_drop_ship_pos(order, user=user, date=d(10, 2)) if not order.purchase_orders.exists() else (
        order.purchase_orders.get(),)
    line = po.lines.get()
    return bill_from_po(po, date=day, vendor_invoice_number=f"V-{suffix}", quantities={line.pk: qty},
                        prices={line.pk: D(price)}, user=user)


@pytest.fixture
def stocked(gadget, vendor, admin_user):
    receive(gadget, 10, "4.00", d(9, 29), vendor, admin_user)
    return gadget


def test_stock_order(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    inv = invoice(so, admin_user, d(10, 5), 3)
    p = order_profit(so.number, d(10, 31))
    assert (p.key, p.sales_order, p.invoice, p.customer, p.date) == (so.number, so, None, customer, d(10, 1))
    assert (p.sales, p.credits, p.cost, p.profit) == (D("28.50"), D("0.00"), D("12.00"), D("16.50"))
    assert (p.rate, p.owed, p.billed, p.balance) == (D("0.5500"), D("9.08"), D("0.00"), D("9.08"))
    assert p.net_sales == D("28.50") and p.margin == D("57.9")
    assert p.flags == []
    (ip,) = p.invoices
    assert (ip.invoice, ip.sales, ip.credits) == (inv, D("28.50"), D("0.00"))
    (lc,) = ip.lines
    assert (lc.kind, lc.cost, lc.pending) == ("stock", D("12.00"), False)


def test_drop_ship_order_with_bill(customer, admin_user, gadget):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 3), 3)
    drop_ship_bill(so, admin_user, d(10, 8), 3, "4.00")
    p = order_profit(so.number, d(10, 31))
    assert (p.sales, p.cost, p.profit, p.owed) == (D("28.50"), D("12.00"), D("16.50"), D("9.08"))
    (lc,) = p.invoices[0].lines
    assert (lc.kind, lc.cost, lc.pending) == ("drop_ship", D("12.00"), False)
    assert p.flags == []


def test_drop_ship_line_without_bill_is_pending(customer, admin_user, gadget):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 3), 3)
    p = order_profit(so.number, d(10, 31))
    assert (p.sales, p.cost, p.profit) == (D("28.50"), D("0.00"), D("28.50"))
    assert p.flags == ["Drop-ship bill not entered"]
    assert p.invoices[0].lines[0].pending is True


def test_partial_drop_ship_bill_is_still_pending(customer, admin_user, gadget):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 3), 3)
    drop_ship_bill(so, admin_user, d(10, 8), 2, "4.00")
    p = order_profit(so.number, d(10, 31))
    assert p.cost == D("8.00")
    assert p.flags == ["Drop-ship bill not entered"]


def test_mixed_order_over_two_invoices(customer, admin_user, stocked, part):
    so = order_of(customer, admin_user, d(10, 1),
                  OrderLineSpec(stocked, 3, D("9.50")), OrderLineSpec(part, 3, D("20.00"), **DS))
    first = invoice(so, admin_user, d(10, 3), 1, 1)
    second = invoice(so, admin_user, d(10, 4), 2, 2)
    drop_ship_bill(so, admin_user, d(10, 8), 3, "7.00")
    p = order_profit(so.number, d(10, 31))
    assert [ip.invoice for ip in p.invoices] == [first, second]
    assert [ip.sales for ip in p.invoices] == [D("29.50"), D("59.00")]
    assert (p.sales, p.cost, p.profit) == (D("88.50"), D("33.00"), D("55.50"))
    assert [(lc.kind, lc.cost) for ip in p.invoices for lc in ip.lines] == [
        ("stock", D("4.00")), ("drop_ship", D("7.00")), ("stock", D("8.00")), ("drop_ship", D("14.00"))]
    assert p.flags == []


def test_direct_invoice_is_its_own_order(customer, admin_user, stocked):
    add_rate(effective_from=d(10, 5), percent=D("50"), user=admin_user)
    inv = create_invoice(customer=customer, date=d(10, 6), user=admin_user,
                         lines=[InvoiceLineSpec(stocked, 3, D("9.50"))])
    p = order_profit(inv.number, d(10, 31))
    assert (p.key, p.sales_order, p.invoice, p.date) == (inv.number, None, inv, d(10, 6))
    assert (p.profit, p.rate, p.owed) == (D("16.50"), D("0.5000"), D("8.25"))


def test_edited_invoice_counts_current_lines(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    inv = invoice(so, admin_user, d(10, 5), 3)
    edit_invoice(inv, user=admin_user, date=d(10, 5), terms=None, ship_to=inv.ship_to, notes="", lines=[
        InvoiceLineSpec(stocked, 2, D("9.50"), so_line=so.lines.get())])
    p = order_profit(so.number, d(10, 31))
    assert (p.sales, p.cost, p.profit) == (D("19.00"), D("8.00"), D("11.00"))


def test_voided_invoice_counts_zero_from_void_date(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    inv = invoice(so, admin_user, d(10, 5), 3)
    void_invoice(inv, user=admin_user, reason="Wrong customer", date=d(10, 10))
    before = order_profit(so.number, d(10, 9))
    assert (before.sales, before.cost, before.profit) == (D("28.50"), D("12.00"), D("16.50"))
    after = order_profit(so.number, d(10, 10))
    assert (after.sales, after.credits, after.cost, after.profit, after.owed) == (
        D("0.00"), D("0.00"), D("0.00"), D("0.00"), D("0.00"))


def credit(customer, user, amount="5.00"):
    return create_credit_note(customer=customer, date=d(10, 6), user=user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D(amount))])


def test_credit_applied_reduces_sales(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    inv = invoice(so, admin_user, d(10, 5), 3)
    apply_credit(credit(customer, admin_user), inv, D("5.00"), date=d(10, 7), user=admin_user)
    assert order_profit(so.number, d(10, 6)).profit == D("16.50")
    p = order_profit(so.number, d(10, 7))
    assert (p.sales, p.credits, p.net_sales, p.profit) == (D("28.50"), D("5.00"), D("23.50"), D("11.50"))
    assert p.invoices[0].credits == D("5.00")


def test_credit_removed_then_applied_to_other_order(customer, admin_user, stocked):
    a = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    b = order_of(customer, admin_user, d(10, 2), OrderLineSpec(stocked, 3, D("9.50")))
    inv_a = invoice(a, admin_user, d(10, 5), 3)
    inv_b = invoice(b, admin_user, d(10, 5), 3)
    cn = credit(customer, admin_user)
    app = apply_credit(cn, inv_a, D("5.00"), date=d(10, 7), user=admin_user)
    remove_application(app, date=d(10, 9), user=admin_user)
    apply_credit(cn, inv_b, D("5.00"), date=d(10, 9), user=admin_user)
    assert (order_profit(a.number, d(10, 8)).credits, order_profit(b.number, d(10, 8)).credits) == (
        D("5.00"), D("0.00"))
    assert (order_profit(a.number, d(10, 9)).credits, order_profit(b.number, d(10, 9)).credits) == (
        D("0.00"), D("5.00"))


def test_fifo_replay_changes_cost(customer, admin_user, gadget, vendor):
    receive(gadget, 10, "4.00", d(10, 3), vendor, admin_user)
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50")))
    invoice(so, admin_user, d(10, 5), 3)
    assert order_profit(so.number, d(10, 31)).cost == D("12.00")
    receive(gadget, 10, "3.00", d(10, 2), vendor, admin_user)  # backdated and cheaper
    p = order_profit(so.number, d(10, 31))
    assert (p.cost, p.profit) == (D("9.00"), D("19.50"))


def test_as_of_excludes_later_documents(customer, admin_user, gadget):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 3), 1)
    drop_ship_bill(so, admin_user, d(10, 11), 3, "4.00")
    invoice(so, admin_user, d(10, 12), 2)
    p = order_profit(so.number, d(10, 10))
    assert (len(p.invoices), p.sales, p.cost) == (1, D("9.50"), D("0.00"))
    assert p.flags == ["Drop-ship bill not entered"]
    p = order_profit(so.number, d(10, 11))
    assert (len(p.invoices), p.sales, p.cost, p.flags) == (1, D("9.50"), D("4.00"), [])
    p = order_profit(so.number, d(10, 12))
    assert (len(p.invoices), p.sales, p.cost, p.flags) == (2, D("28.50"), D("12.00"), [])
    assert [o.key for o in orders(d(10, 2))] == []


def test_loss_owes_zero(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("3.00")))
    invoice(so, admin_user, d(10, 5), 3)
    p = order_profit(so.number, d(10, 31))
    assert (p.profit, p.owed, p.margin) == (D("-3.00"), D("0.00"), D("-33.3"))
    assert p.flags == ["Below zero, no handling"]


def test_rate_by_order_date(customer, admin_user, stocked):
    add_rate(effective_from=d(11, 1), percent=D("50"), user=admin_user)
    so = order_of(customer, admin_user, d(10, 31), OrderLineSpec(stocked, 3, D("9.50")))
    invoice(so, admin_user, d(11, 3), 3)
    p = order_profit(so.number, d(11, 3))
    assert (p.rate, p.owed) == (D("0.5500"), D("9.08"))


def test_before_start_owes_zero(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(9, 30), OrderLineSpec(stocked, 3, D("9.50")))
    invoice(so, admin_user, d(10, 2), 3)
    p = order_profit(so.number, d(10, 31))
    assert (p.profit, p.owed) == (D("16.50"), D("0.00"))
    assert p.flags == ["Before handling start"]


def test_no_rate_owes_zero(customer, admin_user, stocked, handling_setup):
    handling_setup.start_date = d(9, 1)
    handling_setup.save()
    so = order_of(customer, admin_user, d(9, 30), OrderLineSpec(stocked, 3, D("9.50")))
    invoice(so, admin_user, d(10, 2), 3)
    p = order_profit(so.number, d(10, 31))
    assert (p.rate, p.owed, p.flags) == (None, D("0.00"), [])


def test_orders_filters_by_date_and_customer(customer, admin_user, stocked):
    other = Customer.objects.create(name="Beta Co", terms=Terms.NET_30)
    a = order_of(customer, admin_user, d(10, 2), OrderLineSpec(stocked, 1, D("9.50")))
    b = order_of(other, admin_user, d(10, 5), OrderLineSpec(stocked, 1, D("9.50")))
    c = order_of(customer, admin_user, d(10, 9), OrderLineSpec(stocked, 1, D("9.50")))
    invoice(a, admin_user, d(10, 3), 1)
    invoice(b, admin_user, d(10, 6), 1)
    direct = create_invoice(customer=customer, date=d(10, 8), user=admin_user,
                            lines=[InvoiceLineSpec(stocked, 1, D("9.50"))])
    invoice(c, admin_user, d(10, 20), 1)
    as_of = d(10, 15)
    assert [o.key for o in orders(as_of)] == [a.number, b.number, direct.number]
    assert [o.key for o in orders(as_of, date_from=d(10, 3))] == [b.number, direct.number]
    assert [o.key for o in orders(as_of, date_to=d(10, 4))] == [a.number]
    assert [o.key for o in orders(as_of, customer=customer)] == [a.number, direct.number]
    assert [o.key for o in orders(d(10, 31))] == [a.number, b.number, direct.number, c.number]
    assert [o.profit for o in orders(as_of)] == [order_profit(k, as_of).profit for k in (a.number, b.number, direct.number)]


def test_billed_to_date_counts_posted_runs_only(customer, admin_user, stocked):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("9.50")))
    invoice(so, admin_user, d(10, 5), 3)
    values = dict(sales_order=so, customer=customer, sales=D("28.50"), cost=D("12.00"), profit=D("16.50"),
                  rate=D("0.5500"), owed_to_date=D("9.08"), billed_before=D("0.00"))
    for start, end, status, amount in [
        (d(10, 1), d(10, 15), RunStatus.VOID, "9.08"),
        (d(10, 1), d(10, 15), RunStatus.POSTED, "9.00"),
        (d(10, 16), d(10, 31), RunStatus.POSTED, "0.08"),
    ]:
        run = HandlingRun.objects.create(period_start=start, period_end=end, total=D(amount), status=status,
                                         statement_pdf="handling/x.pdf", created_by=admin_user)
        HandlingRunLine.objects.create(run=run, order_key=so.number, this_bill=D(amount), **values)
    assert billed_to_date(so.number) == D("9.08")
    assert billed_to_date("SO-99999") == D("0.00")
    p = order_profit(so.number, d(10, 31))
    assert (p.billed, p.balance) == (D("9.08"), D("0.00"))
    assert orders(d(10, 31))[0].billed == D("9.08")


def test_orders_query_count_does_not_grow_per_order(customer, admin_user, stocked, part):
    def make_order(day):
        so = order_of(customer, admin_user, d(10, day),
                      OrderLineSpec(stocked, 1, D("9.50")), OrderLineSpec(part, 1, D("20.00"), **DS))
        invoice(so, admin_user, d(10, day), 1, 1)
        drop_ship_bill(so, admin_user, d(10, day), 1, "7.00", suffix=str(day))

    make_order(3)
    with CaptureQueriesContext(connection) as one:
        assert len(orders(d(10, 31))) == 1
    for day in (4, 5, 6):
        make_order(day)
    with CaptureQueriesContext(connection) as four:
        assert len(orders(d(10, 31))) == 4
    assert len(four.captured_queries) == len(one.captured_queries) <= 12

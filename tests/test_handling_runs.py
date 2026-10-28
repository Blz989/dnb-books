import importlib
import re
import threading
from datetime import date
from decimal import Decimal

import pytest
from django.core.management.color import no_style
from django.db import connection, transaction

from contacts.models import Vendor
from handling.billing import HandlingError, create_run, preview, render_statement_html, statement_pdf
from handling.models import HandlingRun, HandlingSettings, RunStatus
from handling.periods import Period, due_periods, next_to_bill
from ledger.models import JournalLine
from purchasing.checks import ap_check
from purchasing.models import VendorCreditKind
from purchasing.posting.bills import bill_from_po
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from sales.models import Fulfillment
from sales.posting.invoices import invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note, remove_application

D = Decimal
DS = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")
OCT_1 = Period(date(2026, 10, 1), date(2026, 10, 15))
OCT_2 = Period(date(2026, 10, 16), date(2026, 10, 31))
NOV_1 = Period(date(2026, 11, 1), date(2026, 11, 15))
LATER = date(2026, 12, 31)


def d(month, day):
    return date(2026, month, day)


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def setup(handling_setup, inv_settings, purchasing_settings, sales_settings):
    Vendor.objects.create(name="Acme Mfg")
    return handling_setup


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


@pytest.fixture
def stocked(gadget, part, vendor, admin_user):
    receive(gadget, 30, "4.00", d(9, 29), vendor, admin_user)
    receive(part, 30, "7.00", d(9, 29), vendor, admin_user)
    return gadget


def order_of(customer, user, day, *lines):
    return create_order(customer=customer, date=day, user=user, lines=list(lines))


def invoice(order, user, day, *qtys):
    lines = list(order.lines.all())
    return invoice_from_order(order, date=day, quantities={l.pk: q for l, q in zip(lines, qtys)}, user=user)


def stock_sale(customer, user, item, order_day, invoice_day, qty=3, price="9.50"):
    so = order_of(customer, user, order_day, OrderLineSpec(item, qty, D(price)))
    return so, invoice(so, user, invoice_day, qty)


def credit_note(customer, user, day, amount):
    return create_credit_note(customer=customer, date=day, user=user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D(amount))])


@pytest.fixture
def purge_documents():
    """For the threaded test: empty every table in one transaction, with the immutability triggers off.

    ledger_purge deletes journal entries outside a transaction, which the (deferred) foreign keys from invoices and
    bills refuse. Request this after ledger_purge so it tears down first and leaves ledger_purge nothing to do.
    """
    yield
    migs = [importlib.import_module(f"{app}.migrations.0002_immutability_triggers") for app in ("ledger", "items")]
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    with transaction.atomic(), connection.cursor() as c:
        for mig in migs:
            for sql in mig.REVERSE:
                c.execute(sql)
        for sql in connection.ops.sql_flush(no_style(), tables):
            c.execute(sql)
        for mig in migs:
            for sql in mig.FORWARD:
                c.execute(sql)


def row_for(run_or_preview, key):
    rows = run_or_preview.rows if hasattr(run_or_preview, "rows") else run_or_preview.lines.all()
    (row,) = [r for r in rows if (r.order.key if hasattr(r, "order") else r.order_key) == key]
    return row


# ---- periods ----

def test_periods_and_labels():
    p = Period.containing(date(2026, 2, 20))
    assert (p.start, p.end, p.label) == (date(2026, 2, 16), date(2026, 2, 28), "Feb 16–28, 2026")
    assert Period.containing(date(2026, 10, 15)) == OCT_1
    assert OCT_1.label == "Oct 1–15, 2026" and OCT_2.label == "Oct 16–31, 2026"
    assert OCT_1.next() == OCT_2
    assert OCT_2.next() == NOV_1
    assert Period.containing(date(2026, 12, 20)).next() == Period(date(2027, 1, 1), date(2027, 1, 15))
    with pytest.raises(Exception):
        OCT_1.start = date(2026, 10, 2)  # frozen


def test_first_period_contains_start_date(setup):
    assert next_to_bill() == OCT_1
    setup.start_date = d(10, 20)
    setup.save()
    assert next_to_bill() == OCT_2
    assert due_periods(d(10, 30)) == []
    assert due_periods(d(11, 15)) == [OCT_2, NOV_1]


def test_next_to_bill_none_until_configured(db):
    assert next_to_bill() is None
    assert due_periods(d(12, 31)) == []


def test_due_periods_follow_latest_run(customer, admin_user, stocked):
    assert due_periods(d(10, 14)) == []
    assert due_periods(d(10, 31)) == [OCT_1, OCT_2]
    create_run(OCT_1, user=admin_user, today=LATER)
    assert next_to_bill() == OCT_2
    assert due_periods(d(11, 15)) == [OCT_2, NOV_1]


# ---- creating a run ----

def test_create_run_posts_one_bill_for_the_total(customer, admin_user, stocked, part, setup):
    a, _ = stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))  # profit 16.50, owed 9.08
    b, _ = stock_sale(customer, admin_user, part, d(10, 2), d(10, 6), price="20.00")  # profit 39.00, owed 21.45
    stock_sale(customer, admin_user, stocked, d(10, 3), d(10, 20))  # invoiced after the period: not counted
    pv = preview(OCT_1)
    assert [r.order.key for r in pv.rows] == [a.number, b.number]
    assert pv.total == D("30.53")

    run = create_run(OCT_1, user=admin_user, today=d(10, 15))
    assert (run.period_start, run.period_end, run.total, run.status) == (d(10, 1), d(10, 15), D("30.53"),
                                                                         RunStatus.POSTED)
    assert run.created_by == admin_user and run.vendor_credit is None
    bill = run.bill
    assert (bill.vendor, bill.date, bill.vendor_invoice_number) == (setup.vendor, d(10, 15), "HND-20261015")
    assert bill.notes == "Handling statement Oct 1–15, 2026"
    assert not bill.item_lines.exists()
    (line,) = bill.expense_lines.all()
    assert (line.account.number, line.amount, line.description) == ("6400", D("30.53"), "Handling Oct 1–15, 2026")
    lines = {(l.account.number, l.debit, l.credit) for l in JournalLine.objects.filter(entry=bill.journal_entry)}
    assert lines == {("6400", D("30.53"), D("0.00")), ("2000", D("0.00"), D("30.53"))}

    got = [(l.order_key, l.sales_order, l.invoice, l.customer, l.sales, l.cost, l.profit, l.rate, l.owed_to_date,
            l.billed_before, l.this_bill) for l in run.lines.order_by("order_key")]
    want = [(r.order.key, r.order.sales_order, r.order.invoice, r.order.customer, r.order.net_sales, r.order.cost,
             r.order.profit, r.order.rate, r.owed_to_date, r.billed_before, r.this_bill) for r in pv.rows]
    assert got == want
    assert got[0][4:] == (D("28.50"), D("12.00"), D("16.50"), D("0.5500"), D("9.08"), D("0.00"), D("9.08"))
    assert run.statement_pdf.name.startswith("handling/") and run.statement_pdf.read().startswith(b"%PDF")


def test_ap_check_stays_zero_after_run(customer, admin_user, stocked):
    stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))
    run = create_run(OCT_1, user=admin_user, today=LATER)
    assert run.bill is not None
    assert ap_check(OCT_1.end) == []
    assert ap_check() == []


def test_period_must_have_ended(customer, admin_user, stocked):
    stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))
    with pytest.raises(HandlingError) as e:
        create_run(OCT_1, user=admin_user, today=d(10, 14))
    assert str(e.value) == "Oct 1–15, 2026 can't be billed until Oct 15, 2026."
    assert not HandlingRun.objects.exists()
    assert create_run(OCT_1, user=admin_user, today=d(10, 15)).total == D("9.08")


def test_periods_in_order(customer, admin_user, stocked):
    with pytest.raises(HandlingError) as e:
        create_run(OCT_2, user=admin_user, today=LATER)
    assert str(e.value) == "Bill Oct 1–15, 2026 first."
    assert not HandlingRun.objects.exists()


def test_period_billed_once(customer, admin_user, stocked):
    stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))
    create_run(OCT_1, user=admin_user, today=LATER)
    with pytest.raises(HandlingError) as e:
        create_run(OCT_1, user=admin_user, today=LATER)
    assert str(e.value) == "Oct 1–15, 2026 is already billed."
    with pytest.raises(HandlingError) as e:
        create_run(Period(d(9, 16), d(9, 30)), user=admin_user, today=LATER)
    assert str(e.value) == "Sep 16–30, 2026 is already billed."
    assert HandlingRun.objects.count() == 1


def test_zero_total_records_run_without_document(customer, admin_user, stocked):
    run = create_run(OCT_1, user=admin_user, today=LATER)
    assert (run.total, run.bill, run.vendor_credit, run.status) == (D("0.00"), None, None, RunStatus.POSTED)
    assert not run.lines.exists()
    assert run.statement_pdf.read().startswith(b"%PDF")
    assert next_to_bill() == OCT_2


def test_negative_total_creates_vendor_credit(customer, admin_user, stocked, setup):
    so, inv = stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))  # owed 9.08
    create_run(OCT_1, user=admin_user, today=LATER)
    apply_credit(credit_note(customer, admin_user, d(10, 20), "10.00"), inv, D("10.00"), date=d(10, 20),
                 user=admin_user)  # profit 6.50, owed 3.58
    run = create_run(OCT_2, user=admin_user, today=LATER)
    assert (run.total, run.bill) == (D("-5.50"), None)
    row = row_for(run, so.number)
    assert (row.owed_to_date, row.billed_before, row.this_bill) == (D("3.58"), D("9.08"), D("-5.50"))
    vc = run.vendor_credit
    assert (vc.vendor, vc.date, vc.kind, vc.reason) == (setup.vendor, d(10, 31), VendorCreditKind.ALLOWANCE,
                                                        "Handling corrections Oct 16–31, 2026")
    (line,) = vc.lines.all()
    assert (line.account.number, line.amount) == ("6400", D("5.50"))
    lines = {(l.account.number, l.debit, l.credit) for l in JournalLine.objects.filter(entry=vc.journal_entry)}
    assert lines == {("2000", D("5.50"), D("0.00")), ("6400", D("0.00"), D("5.50"))}
    assert ap_check(OCT_2.end) == []


def test_late_drop_ship_bill_corrected_next_run(customer, admin_user, gadget):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 10), 3)
    first = create_run(OCT_1, user=admin_user, today=d(10, 15))
    row = row_for(first, so.number)
    assert (row.cost, row.profit, row.this_bill, first.total) == (D("0.00"), D("28.50"), D("15.68"), D("15.68"))

    # Entered on Oct 20, but dated Oct 12, inside the period already billed.
    (po,) = create_drop_ship_pos(so, user=admin_user, date=d(10, 2))
    line = po.lines.get()
    bill_from_po(po, date=d(10, 12), vendor_invoice_number="V-1", quantities={line.pk: 3},
                 prices={line.pk: D("4.00")}, user=admin_user)
    second = create_run(OCT_2, user=admin_user, today=d(10, 31))
    row = row_for(second, so.number)
    assert (row.cost, row.profit, row.owed_to_date, row.billed_before, row.this_bill) == (
        D("12.00"), D("16.50"), D("9.08"), D("15.68"), D("-6.60"))
    assert second.total == D("-6.60") and second.vendor_credit.lines.get().amount == D("6.60")


def test_credit_moved_between_orders(customer, admin_user, stocked):
    a, inv_a = stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))
    b, inv_b = stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 5))
    cn = credit_note(customer, admin_user, d(10, 6), "5.00")
    app = apply_credit(cn, inv_a, D("5.00"), date=d(10, 7), user=admin_user)
    first = create_run(OCT_1, user=admin_user, today=LATER)
    assert (row_for(first, a.number).this_bill, row_for(first, b.number).this_bill) == (D("6.33"), D("9.08"))

    remove_application(app, date=d(10, 20), user=admin_user)
    apply_credit(cn, inv_b, D("5.00"), date=d(10, 20), user=admin_user)
    pv = preview(OCT_2)
    assert (row_for(pv, a.number).this_bill, row_for(pv, b.number).this_bill) == (D("2.75"), D("-2.75"))
    assert pv.total == D("0.00")
    second = create_run(OCT_2, user=admin_user, today=LATER)
    assert (second.total, second.bill, second.vendor_credit) == (D("0.00"), None, None)
    assert second.lines.count() == 2


def test_loss_then_profit(customer, admin_user, stocked, part):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(stocked, 3, D("3.00")),
                  OrderLineSpec(part, 3, D("20.00")))
    invoice(so, admin_user, d(10, 5), 3, 0)  # sales 9.00, cost 12.00: a loss of 3.00
    first = create_run(OCT_1, user=admin_user, today=LATER)
    assert (first.total, first.bill) == (D("0.00"), None)
    assert not first.lines.exists()

    invoice(so, admin_user, d(10, 20), 0, 3)  # adds sales 60.00, cost 21.00: cumulative profit 36.00
    second = create_run(OCT_2, user=admin_user, today=LATER)
    row = row_for(second, so.number)
    assert (row.profit, row.owed_to_date, row.billed_before, row.this_bill) == (
        D("36.00"), D("19.80"), D("0.00"), D("19.80"))
    assert second.total == D("19.80")


@pytest.mark.django_db(transaction=True)
def test_second_create_for_same_period_refused(customer, admin_user, gadget, ledger_purge, purge_documents):
    so = order_of(customer, admin_user, d(10, 1), OrderLineSpec(gadget, 3, D("9.50"), **DS))
    invoice(so, admin_user, d(10, 10), 3)
    results, errors = [], []
    start = threading.Barrier(2)

    def worker():
        try:
            start.wait()
            results.append(create_run(OCT_1, user=admin_user, today=LATER))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1
    assert [type(e) for e in errors] == [HandlingError]
    assert str(errors[0]) == "Oct 1–15, 2026 is already billed."
    assert HandlingRun.objects.filter(status=RunStatus.POSTED).count() == 1
    assert HandlingRun.objects.get().total == D("15.68")


def test_settings_incomplete_refused(customer, admin_user, stocked, setup):
    s = HandlingSettings.load()
    s.vendor = None
    s.save()
    with pytest.raises(HandlingError) as e:
        create_run(OCT_1, user=admin_user, today=LATER)
    assert str(e.value) == "Set up handling in Purchasing settings first."
    assert not HandlingRun.objects.exists()


def test_statement_pdf_lists_rows(customer, admin_user, stocked, part):
    a, _ = stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))
    b, _ = stock_sale(customer, admin_user, part, d(10, 2), d(10, 6), price="20.00")
    run = create_run(OCT_1, user=admin_user, today=LATER)
    html = render_statement_html(run)
    assert "Handling statement, Oct 1–15, 2026" in html
    assert "DNB Supply" in html and "Harbor Handling LLC" in html
    for key in (a.number, b.number):
        assert key in html
    assert "30.53" in html and "Acme Builders" in html
    pdf = statement_pdf(run)
    assert pdf.startswith(b"%PDF")


def test_preview_rows_sorted_by_order_key_like_run_and_statement(customer, admin_user, stocked):
    later, _ = stock_sale(customer, admin_user, stocked, d(10, 5), d(10, 6))  # SO-00001, dated after the next
    earlier, _ = stock_sale(customer, admin_user, stocked, d(10, 2), d(10, 6))  # SO-00002
    assert later.number < earlier.number
    assert [r.order.key for r in preview(OCT_1).rows] == [later.number, earlier.number]
    run = create_run(OCT_1, user=admin_user, today=LATER)
    assert [l.order_key for l in run.lines.order_by("order_key")] == [later.number, earlier.number]
    html = render_statement_html(run)
    assert html.index(later.number) < html.index(earlier.number)


def test_statement_total_signed_like_rows(customer, admin_user, stocked):
    so, inv = stock_sale(customer, admin_user, stocked, d(10, 1), d(10, 5))  # owed 9.08
    create_run(OCT_1, user=admin_user, today=LATER)
    apply_credit(credit_note(customer, admin_user, d(10, 20), "10.00"), inv, D("10.00"), date=d(10, 20),
                 user=admin_user)  # owed 3.58, this bill -5.50
    run = create_run(OCT_2, user=admin_user, today=LATER)
    html = render_statement_html(run)
    total_row = re.search(r'<tr class="total">.*?</tr>', html, re.S).group(0)
    assert re.search(r'>-5\.50</td>\s*</tr>$', total_row), total_row
    assert "−" not in total_row and "$" not in total_row

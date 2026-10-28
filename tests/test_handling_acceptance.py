"""Spec section 1, success criteria 1 and 2: a scripted October 2026 with both handling runs, every ledger check at
zero after each step, and the handling bills equal to the Handling expense account."""
from datetime import date
from decimal import Decimal

import pytest
from django.db.models import Sum

from contacts.models import Vendor
from handling.billing import create_run
from handling.models import HandlingRun, RunStatus
from handling.periods import Period
from handling.profit import order_profit
from items.stock import check_invariants
from ledger.models import JournalLine
from ledger.periods import close_period
from purchasing.balances import bill_total, credit_total
from purchasing.checks import ap_check
from purchasing.models import VendorPaymentMethod as VM
from purchasing.posting.bills import bill_from_po
from purchasing.posting.orders import POLineSpec, create_drop_ship_pos, create_po
from purchasing.posting.payments import PayLine, pay_bills
from sales.checks import ar_check, undeposited_check
from sales.models import Fulfillment
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note

D = Decimal
DS = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Acme Mfg")
OCT_1 = Period(date(2026, 10, 1), date(2026, 10, 15))
OCT_2 = Period(date(2026, 10, 16), date(2026, 10, 31))


def d(month, day):
    return date(2026, month, day)


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def checks():
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []
    for as_of in (OCT_1.end, OCT_2.end):
        assert ap_check(as_of) == [] and ar_check(as_of) == [] and undeposited_check(as_of) == [], as_of


def receive(item, qty, price, day, vendor, user):
    po = create_po(vendor=vendor, date=day, user=user, lines=[POLineSpec(item, qty, D(price))])
    line = po.lines.get()
    return bill_from_po(po, date=day, vendor_invoice_number=f"R-{po.number}", quantities={line.pk: qty}, user=user)


def invoice(order, user, day, *qtys):
    lines = list(order.lines.all())
    return invoice_from_order(order, date=day, quantities={l.pk: q for l, q in zip(lines, qtys)}, user=user)


def drop_ship_bill(order, user, day, qty, price, ref):
    (po,) = create_drop_ship_pos(order, user=user, date=order.date)
    line = po.lines.get()
    return bill_from_po(po, date=day, vendor_invoice_number=ref, quantities={line.pk: qty},
                        prices={line.pk: D(price)}, user=user)


def rows(run):
    return {l.order_key: (l.sales, l.cost, l.profit, l.rate, l.owed_to_date, l.billed_before, l.this_bill)
            for l in run.lines.all()}


def handling_balance():
    t = JournalLine.objects.in_ledger().filter(account__number="6400").aggregate(dr=Sum("debit"), cr=Sum("credit"))
    return (t["dr"] or D("0.00")) - (t["cr"] or D("0.00"))


def test_handling_month(handling_setup, inv_settings, purchasing_settings, sales_settings, make_item, customer,
                        vendor, admin_user, bank):
    Vendor.objects.create(name="Acme Mfg")
    gadget = make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")
    part = make_item("PART", sale_price="20.0000", purchase_cost="7.0000")
    receive(gadget, 30, "4.00", d(9, 29), vendor, admin_user)
    receive(part, 30, "7.00", d(9, 29), vendor, admin_user)
    checks()

    # 1. Ship and invoice three orders in the first half of October.
    # A, stock: 3 GADGET at 9.50.
    so_a = create_order(customer=customer, date=d(10, 1), user=admin_user,
                        lines=[OrderLineSpec(gadget, 3, D("9.50"))])
    inv_a = invoice(so_a, admin_user, d(10, 5), 3)
    checks()
    # B, drop-ship: 2 PART at 25.00, invoiced before Acme Mfg's bill is entered.
    so_b = create_order(customer=customer, date=d(10, 2), user=admin_user,
                        lines=[OrderLineSpec(part, 2, D("25.00"), **DS)])
    invoice(so_b, admin_user, d(10, 6), 2)
    checks()
    # C, mixed: 4 GADGET from stock at 9.50 and 2 PART drop-shipped at 30.00, shipped in two invoices.
    so_c = create_order(customer=customer, date=d(10, 3), user=admin_user,
                        lines=[OrderLineSpec(gadget, 4, D("9.50")), OrderLineSpec(part, 2, D("30.00"), **DS)])
    invoice(so_c, admin_user, d(10, 7), 2, 1)
    inv_c2 = invoice(so_c, admin_user, d(10, 12), 2, 1)
    drop_ship_bill(so_c, admin_user, d(10, 13), 2, "7.50", "AM-200")
    checks()
    assert order_profit(so_b.number, OCT_1.end).flags == ["Drop-ship bill not entered"]

    # 2. Handling bill for Oct 1-15.
    first = create_run(OCT_1, user=admin_user, today=d(10, 15))
    assert (first.status, first.vendor_credit) == (RunStatus.POSTED, None)
    assert rows(first) == {
        # 28.50 - 12.00 = 16.50; 16.50 x 0.55 = 9.075, half up to 9.08.
        so_a.number: (D("28.50"), D("12.00"), D("16.50"), D("0.5500"), D("9.08"), D("0.00"), D("9.08")),
        # The drop-ship cost counts as 0.00 until its bill is entered.
        so_b.number: (D("50.00"), D("0.00"), D("50.00"), D("0.5500"), D("27.50"), D("0.00"), D("27.50")),
        # Sales 4 x 9.50 + 2 x 30.00 = 98.00; cost 4 x 4.00 + 2 x 7.50 = 31.00.
        so_c.number: (D("98.00"), D("31.00"), D("67.00"), D("0.5500"), D("36.85"), D("0.00"), D("36.85")),
    }
    assert first.total == D("73.43")
    assert bill_total(first.bill) == D("73.43")
    checks()

    # 3. The late drop-ship bill for B (backdated into the billed period), a credit on C, and an edit to A.
    drop_ship_bill(so_b, admin_user, d(10, 14), 2, "8.00", "AM-201")
    checks()
    note = create_credit_note(customer=customer, date=d(10, 20), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("10.00"))])
    apply_credit(note, inv_c2, D("10.00"), date=d(10, 20), user=admin_user)
    checks()
    edit_invoice(inv_a, user=admin_user, date=d(10, 5), terms=None, ship_to=inv_a.ship_to, notes="",
                 reason="Price corrected", lines=[InvoiceLineSpec(gadget, 3, D("12.00"), so_line=so_a.lines.get())])
    checks()
    # D, a direct invoice in the second half: 3 PART from stock at 20.00.
    inv_d = create_invoice(customer=customer, date=d(10, 22), user=admin_user,
                           lines=[InvoiceLineSpec(part, 3, D("20.00"))])
    checks()

    # 4. Handling bill for Oct 16-31, with the corrections.
    second = create_run(OCT_2, user=admin_user, today=d(10, 31))
    assert (second.status, second.vendor_credit) == (RunStatus.POSTED, None)
    assert rows(second) == {
        # Edited to 3 x 12.00 = 36.00; profit 24.00, owed 13.20; 13.20 - 9.08 = 4.12.
        so_a.number: (D("36.00"), D("12.00"), D("24.00"), D("0.5500"), D("13.20"), D("9.08"), D("4.12")),
        # Late bill 2 x 8.00 = 16.00; profit 34.00, owed 18.70; 18.70 - 27.50 = -8.80.
        so_b.number: (D("50.00"), D("16.00"), D("34.00"), D("0.5500"), D("18.70"), D("27.50"), D("-8.80")),
        # Credit 10.00: sales 88.00, profit 57.00, owed 31.35; 31.35 - 36.85 = -5.50.
        so_c.number: (D("88.00"), D("31.00"), D("57.00"), D("0.5500"), D("31.35"), D("36.85"), D("-5.50")),
        # 60.00 - 3 x 7.00 = 39.00; owed 21.45.
        inv_d.number: (D("60.00"), D("21.00"), D("39.00"), D("0.5500"), D("21.45"), D("0.00"), D("21.45")),
    }
    # 4.12 - 8.80 - 5.50 + 21.45 = 11.27
    assert second.total == D("11.27")
    assert bill_total(second.bill) == D("11.27")
    checks()

    # 5. Pay both handling bills, then close October.
    (payment,) = pay_bills(date=d(10, 31), method=VM.ACH, paid_from=bank, user=admin_user,
                           lines=[PayLine(first.bill, D("73.43")), PayLine(second.bill, D("11.27"))])
    assert payment.vendor == handling_setup.vendor
    checks()
    period = close_period(2026, 10, admin_user)
    assert period.is_closed and period.close_override_reason == ""
    checks()

    # Success criterion 2: handling billed (bills less vendor credits) equals the Handling expense account.
    runs = HandlingRun.objects.filter(status=RunStatus.POSTED)
    billed = sum((bill_total(r.bill) for r in runs if r.bill), D("0.00")) - sum(
        (credit_total(r.vendor_credit) for r in runs if r.vendor_credit), D("0.00"))
    assert billed == D("84.70")  # 73.43 + 11.27
    assert handling_balance() == billed

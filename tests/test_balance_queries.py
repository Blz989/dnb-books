"""The report queries only load documents that can have a balance. These tests rebuild every number the slow way
(every document, Python balance math) and require the fast path to match, for today and for dates in the past."""
from datetime import date
from decimal import Decimal

import pytest

from contacts.models import Customer, Vendor
from purchasing import balances as pbal
from purchasing import reports as preports
from purchasing.checks import vendor_balances_total
from purchasing.models import (
    Bill, BillExpenseLine, BillPaymentApplication, VendorCredit, VendorCreditApplication, VendorCreditLine,
    VendorPayment,
)
from sales import balances as sbal
from sales import reports as sreports
from sales.checks import customer_balances_total
from sales.models import (
    CreditApplication, CreditNote, CreditNoteLine, DocStatus, Invoice, InvoiceLine, Payment, PaymentApplication,
    Refund,
)

D = Decimal
AS_OFS = [None, date(2026, 1, 5), date(2026, 2, 10), date(2026, 3, 1), date(2026, 3, 20), date(2026, 6, 1)]


def d(m, day):
    return date(2026, m, day)


@pytest.fixture
def sales_data(db, admin_user, customer, make_item, cash):
    item = make_item("SKU")
    other = Customer.objects.create(name="Beta Co")
    n = iter(range(1, 1000))

    def invoice(cust, on, amounts, **kw):
        inv = Invoice.objects.create(number=f"I{next(n)}", customer=cust, date=on, due_date=on, created_by=admin_user, **kw)
        for a in amounts:
            InvoiceLine.objects.create(invoice=inv, item=item, quantity=1, price=D(a), amount=D(a))
        return inv

    def payment(cust, on, amount, **kw):
        return Payment.objects.create(number=f"P{next(n)}", customer=cust, date=on, method="check", amount=D(amount),
                                      deposited_to=cash, created_by=admin_user, **kw)

    def credit(cust, on, amounts, **kw):
        c = CreditNote.objects.create(number=f"C{next(n)}", customer=cust, date=on, created_by=admin_user, **kw)
        for a in amounts:
            CreditNoteLine.objects.create(credit_note=c, account=cash, amount=D(a))
        return c

    open_inv = invoice(customer, d(1, 2), ["100.00", "25.50"])
    paid_inv = invoice(customer, d(1, 3), ["80.00"])
    part_inv = invoice(other, d(2, 1), ["60.00"])
    voided = invoice(other, d(1, 20), ["40.00"], status=DocStatus.VOID, voided_on=d(3, 1))
    void_unknown = invoice(other, d(1, 21), ["10.00"], status=DocStatus.VOID)
    pay = payment(customer, d(1, 10), "200.00")
    PaymentApplication.objects.create(payment=pay, invoice=paid_inv, amount=D("80.00"), applied_on=d(1, 10))
    PaymentApplication.objects.create(payment=pay, invoice=open_inv, amount=D("20.00"), applied_on=d(2, 15))
    removed = payment(other, d(2, 2), "30.00")
    PaymentApplication.objects.create(payment=removed, invoice=part_inv, amount=D("30.00"), applied_on=d(2, 2),
                                      removed=True, removed_on=d(3, 10))
    PaymentApplication.objects.create(payment=removed, invoice=part_inv, amount=D("10.00"), applied_on=d(2, 3),
                                      removed=True)
    payment(other, d(2, 5), "15.00", status=DocStatus.VOID, voided_on=d(2, 6))
    note = credit(customer, d(2, 20), ["50.00"])
    CreditApplication.objects.create(credit_note=note, invoice=open_inv, amount=D("30.00"), applied_on=d(3, 5))
    Refund.objects.create(number="R1", customer=customer, date=d(3, 7), bank_account=cash, method="check",
                          amount=D("5.00"), source_payment=pay, created_by=admin_user)
    Refund.objects.create(number="R2", customer=customer, date=d(3, 8), bank_account=cash, method="check",
                          amount=D("5.00"), source_credit_note=note, status=DocStatus.VOID, voided_on=d(3, 9),
                          created_by=admin_user)
    credit(other, d(1, 30), ["12.00"], status=DocStatus.VOID, voided_on=d(2, 1))
    return {"customer": customer, "other": other}


@pytest.mark.parametrize("as_of", AS_OFS)
def test_sales_candidates_cover_every_nonzero_document(sales_data, as_of):
    slow_inv = {i.pk for i in sbal.invoices_with_balances() if sbal.invoice_balance(i, as_of) != 0}
    slow_pay = {p.pk for p in sbal.payments_with_balances() if sbal.payment_unapplied(p, as_of) != 0}
    slow_cn = {c.pk for c in sbal.credit_notes_with_balances() if sbal.credit_unapplied(c, as_of) != 0}
    assert slow_inv <= {i.pk for i in sbal.invoices_with_open_balance(as_of)}
    assert slow_pay <= {p.pk for p in sbal.payments_with_unapplied(as_of)}
    assert slow_cn <= {c.pk for c in sbal.credit_notes_with_unapplied(as_of)}


def test_sales_candidates_skip_settled_documents(sales_data):
    numbers = {i.number for i in sbal.invoices_with_open_balance(None)}
    assert numbers == {"I1", "I3"}  # paid and voided invoices are left out
    assert Invoice.objects.count() == 5


@pytest.mark.parametrize("as_of", AS_OFS)
def test_customer_balances_match_slow_math(sales_data, as_of):
    expected = {}
    for i in sbal.invoices_with_balances():
        expected[i.customer_id] = expected.get(i.customer_id, 0) + sbal.invoice_balance(i, as_of)
    for p in sbal.payments_with_balances():
        expected[p.customer_id] = expected.get(p.customer_id, 0) - sbal.payment_unapplied(p, as_of)
    for c in sbal.credit_notes_with_balances():
        expected[c.customer_id] = expected.get(c.customer_id, 0) - sbal.credit_unapplied(c, as_of)
    expected = {k: v for k, v in expected.items() if v != 0}
    got = {r.customer.pk: r.balance for r in sreports.customer_balances(as_of)}
    assert got == expected
    assert customer_balances_total(as_of) == sum(expected.values(), D("0.00"))
    for cust in Customer.objects.all():
        assert sbal.customer_balance(cust, as_of) == expected.get(cust.pk, D("0.00"))


@pytest.mark.parametrize("as_of", [a for a in AS_OFS if a])
def test_ar_aging_and_open_invoices_match_slow_math(sales_data, as_of):
    buckets = {}
    for i in sbal.invoices_with_balances():
        bal = sbal.invoice_balance(i, as_of)
        if bal != 0:
            key = (i.customer_id, sreports.bucket_for((as_of - i.due_date).days))
            buckets[key] = buckets.get(key, 0) + bal
    report = sreports.ar_aging(as_of)
    got = {(r.customer.pk, b): getattr(r, b) for r in report.rows for b in sreports.BUCKETS if getattr(r, b) != 0}
    assert got == buckets
    credits = D("0.00")
    for p in sbal.payments_with_balances():
        credits -= sbal.payment_unapplied(p, as_of)
    for c in sbal.credit_notes_with_balances():
        credits -= sbal.credit_unapplied(c, as_of)
    assert report.totals.unapplied_credits == credits
    only = sreports.ar_aging(as_of, sales_data["customer"])
    assert [r.customer.pk for r in only.rows] == [r.customer.pk for r in report.rows if r.customer.pk == sales_data["customer"].pk]
    slow_open = sorted(
        (i.pk, sbal.invoice_balance(i, as_of)) for i in sbal.invoices_with_balances() if sbal.invoice_balance(i, as_of) > 0)
    assert sorted((r.invoice.pk, r.balance) for r in sreports.open_invoices(as_of)) == slow_open


@pytest.fixture
def purchasing_data(db, admin_user, vendor, make_item, cash, expense):
    other = Vendor.objects.create(name="Beta Supply")
    n = iter(range(1, 1000))

    def bill(v, on, amounts, **kw):
        b = Bill.objects.create(number=f"B{next(n)}", vendor=v, date=on, due_date=on, created_by=admin_user, **kw)
        for a in amounts:
            BillExpenseLine.objects.create(bill=b, account=expense, amount=D(a))
        return b

    def vpay(v, on, amount, **kw):
        return VendorPayment.objects.create(number=f"V{next(n)}", vendor=v, date=on, method="check", paid_from=cash,
                                            amount=D(amount), created_by=admin_user, **kw)

    open_bill = bill(vendor, d(1, 2), ["100.00", "20.00"])
    paid = bill(vendor, d(1, 3), ["80.00"])
    part = bill(other, d(2, 1), ["60.00"])
    bill(other, d(1, 20), ["40.00"], status=DocStatus.VOID, voided_on=d(3, 1))
    pay = vpay(vendor, d(1, 10), "150.00")
    BillPaymentApplication.objects.create(payment=pay, bill=paid, amount=D("80.00"), applied_on=d(1, 10))
    BillPaymentApplication.objects.create(payment=pay, bill=open_bill, amount=D("20.00"), applied_on=d(2, 15))
    gone = vpay(other, d(2, 2), "30.00")
    BillPaymentApplication.objects.create(payment=gone, bill=part, amount=D("30.00"), applied_on=d(2, 2),
                                          removed=True, removed_on=d(3, 10))
    vpay(other, d(2, 5), "15.00", status=DocStatus.VOID, voided_on=d(2, 6))
    credit = VendorCredit.objects.create(number="VC1", vendor=vendor, date=d(2, 20), kind="return", created_by=admin_user)
    VendorCreditLine.objects.create(credit=credit, account=expense, amount=D("50.00"))
    VendorCreditApplication.objects.create(credit=credit, bill=open_bill, amount=D("30.00"), applied_on=d(3, 5))
    return {"vendor": vendor, "other": other}


@pytest.mark.parametrize("as_of", AS_OFS)
def test_vendor_balances_match_slow_math(purchasing_data, as_of):
    expected = {}
    for b in pbal.bills_with_balances():
        expected[b.vendor_id] = expected.get(b.vendor_id, 0) + pbal.bill_balance(b, as_of)
    for p in pbal.vendor_payments_with_balances():
        expected[p.vendor_id] = expected.get(p.vendor_id, 0) - pbal.vendor_payment_unapplied(p, as_of)
    for c in pbal.vendor_credits_with_balances():
        expected[c.vendor_id] = expected.get(c.vendor_id, 0) - pbal.vendor_credit_unapplied(c, as_of)
    expected = {k: v for k, v in expected.items() if v != 0}
    assert {r.vendor.pk: r.balance for r in preports.vendor_balances(as_of)} == expected
    assert vendor_balances_total(as_of) == sum(expected.values(), D("0.00"))
    for v in Vendor.objects.all():
        assert pbal.vendor_balance(v, as_of) == expected.get(v.pk, D("0.00"))


@pytest.mark.parametrize("as_of", [a for a in AS_OFS if a])
def test_ap_aging_and_bills_due_match_slow_math(purchasing_data, as_of):
    buckets = {}
    for b in pbal.bills_with_balances():
        bal = pbal.bill_balance(b, as_of)
        if bal != 0:
            key = (b.vendor_id, sreports.bucket_for((as_of - b.due_date).days))
            buckets[key] = buckets.get(key, 0) + bal
    report = preports.ap_aging(as_of)
    got = {(r.vendor.pk, k): getattr(r, k) for r in report.rows for k in sreports.BUCKETS if getattr(r, k) != 0}
    assert got == buckets
    slow_due = sorted(
        (b.pk, pbal.bill_balance(b)) for b in pbal.bills_with_balances().filter(status=DocStatus.OPEN)
        if b.due_date <= as_of and pbal.bill_balance(b) > 0)
    assert sorted((r.bill.pk, r.balance) for r in preports.bills_due(as_of)) == slow_due
    assert slow_due  # the scenario has open bills


def test_purchasing_candidates_skip_settled_documents(purchasing_data):
    for as_of in AS_OFS:
        slow = {b.pk for b in pbal.bills_with_balances() if pbal.bill_balance(b, as_of) != 0}
        assert slow <= {b.pk for b in pbal.bills_with_open_balance(as_of)}
        slow = {p.pk for p in pbal.vendor_payments_with_balances() if pbal.vendor_payment_unapplied(p, as_of) != 0}
        assert slow <= {p.pk for p in pbal.vendor_payments_with_unapplied(as_of)}
        slow = {c.pk for c in pbal.vendor_credits_with_balances() if pbal.vendor_credit_unapplied(c, as_of) != 0}
        assert slow <= {c.pk for c in pbal.vendor_credits_with_unapplied(as_of)}
    assert len(list(pbal.bills_with_open_balance(None))) == 2

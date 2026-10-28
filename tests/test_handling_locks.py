"""Task 4: voiding handling runs, the lock on handling bills and credits, and the month close check."""
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from contacts.models import Vendor
from handling.billing import HandlingError, create_run, preview, void_run
from handling.checks import close_check, run_lock
from handling.models import HandlingRun, RunStatus
from handling.periods import Period, next_to_bill
from ledger.errors import PeriodCloseBlockedError, PostingError
from ledger.periods import close_period
from purchasing.errors import PurchasingError
from purchasing.models import VendorPaymentMethod
from purchasing.posting.bills import BillExpenseSpec, bill_from_po, create_bill, edit_bill, void_bill
from purchasing.posting.credits import void_vendor_credit
from purchasing.posting.orders import POLineSpec, create_po
from purchasing.posting.payments import PayLine, pay_bills
from sales.models import DocStatus
from sales.posting.invoices import invoice_from_order
from sales.posting.orders import OrderLineSpec, create_order
from sales.posting.receipts import CreditLineSpec, apply_credit, create_credit_note

D = Decimal
OCT_1 = Period(date(2026, 10, 1), date(2026, 10, 15))
OCT_2 = Period(date(2026, 10, 16), date(2026, 10, 31))
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
def gadget(setup, make_item, vendor, admin_user):
    item = make_item("GADGET", sale_price="9.5000", purchase_cost="4.0000")
    po = create_po(vendor=vendor, date=d(9, 29), user=admin_user, lines=[POLineSpec(item, 30, D("4.00"))])
    line = po.lines.get()
    bill_from_po(po, date=d(9, 29), vendor_invoice_number="R-1", quantities={line.pk: 30}, user=admin_user)
    return item


def sale(customer, user, item, order_day, invoice_day, qty=3):
    so = create_order(customer=customer, date=order_day, user=user, lines=[OrderLineSpec(item, qty, D("9.50"))])
    line = so.lines.get()
    return so, invoice_from_order(so, date=invoice_day, quantities={line.pk: qty}, user=user)


@pytest.fixture
def billed(customer, admin_user, gadget):
    """One order (owed 9.08) billed in Oct 1-15."""
    so, inv = sale(customer, admin_user, gadget, d(10, 1), d(10, 5))
    run = create_run(OCT_1, user=admin_user, today=LATER)
    assert run.bill is not None and run.total == D("9.08")
    return run


@pytest.fixture
def credit_run(customer, admin_user, gadget):
    """Oct 1-15 bills 9.08; a credit note after that turns Oct 16-31 into a vendor credit for 5.50."""
    so, inv = sale(customer, admin_user, gadget, d(10, 1), d(10, 5))
    create_run(OCT_1, user=admin_user, today=LATER)
    note = create_credit_note(customer=customer, date=d(10, 20), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("10.00"))])
    apply_credit(note, inv, D("10.00"), date=d(10, 20), user=admin_user)
    run = create_run(OCT_2, user=admin_user, today=LATER)
    assert run.vendor_credit is not None and run.total == D("-5.50")
    return run


def lock_text(number, label):
    return f"{number} is the handling bill for {label}. Void it from Handling."


# ---- voiding a run ----

def test_void_latest_run_voids_its_bill_and_frees_the_period(billed, admin_user):
    bill = billed.bill
    run = void_run(billed, user=admin_user, reason="Wrong rate")
    assert run.status == RunStatus.VOID
    assert (run.voided_by, run.void_reason) == (admin_user, "Wrong rate")
    assert run.voided_at is not None
    bill.refresh_from_db()
    assert (bill.status, bill.voided_on, bill.void_reason) == (DocStatus.VOID, d(10, 15), "Wrong rate")
    assert next_to_bill() == OCT_1
    # The void run's statement stays viewable.
    assert run.statement_pdf.read().startswith(b"%PDF")


def test_void_only_latest(billed, admin_user):
    second = create_run(OCT_2, user=admin_user, today=LATER)
    with pytest.raises(HandlingError) as e:
        void_run(billed, user=admin_user, reason="Oops")
    assert str(e.value) == "Only the most recent handling bill can be voided."
    billed.refresh_from_db()
    assert billed.status == RunStatus.POSTED and billed.bill.status != DocStatus.VOID

    void_run(second, user=admin_user, reason="Oops")
    with pytest.raises(HandlingError) as e:
        void_run(second, user=admin_user, reason="Again")
    assert str(e.value) == "Only the most recent handling bill can be voided."
    assert void_run(billed, user=admin_user, reason="Oops").status == RunStatus.VOID


def test_void_needs_reason_and_admin(billed, admin_user, standard_user):
    with pytest.raises(HandlingError) as e:
        void_run(billed, user=admin_user, reason="   ")
    assert str(e.value) == "Enter a reason."
    with pytest.raises(PermissionDenied):
        void_run(billed, user=standard_user, reason="Wrong rate")
    billed.refresh_from_db()
    assert billed.status == RunStatus.POSTED


def test_void_refused_when_bill_paid(billed, admin_user, bank):
    bill = billed.bill
    pay_bills(date=d(10, 20), method=VendorPaymentMethod.ACH, paid_from=bank, user=admin_user,
              lines=[PayLine(bill, D("9.08"))])
    with pytest.raises(HandlingError) as e:
        void_run(billed, user=admin_user, reason="Wrong rate")
    assert str(e.value) == f"{bill.number} has payments or credits applied. Remove them first."
    billed.refresh_from_db()
    bill.refresh_from_db()
    assert billed.status == RunStatus.POSTED and billed.void_reason == ""
    assert bill.status != DocStatus.VOID


def test_rebill_after_void(billed, admin_user):
    """Review Focus 1."""
    first_bill, first_total = billed.bill, billed.total
    void_run(billed, user=admin_user, reason="Redo")
    pv = preview(OCT_1)
    assert [r.billed_before for r in pv.rows] == [D("0.00")]
    assert pv.total == first_total

    again = create_run(OCT_1, user=admin_user, today=LATER)
    assert again.pk != billed.pk and again.status == RunStatus.POSTED
    assert again.total == first_total
    assert again.bill.pk != first_bill.pk
    assert again.bill.vendor_invoice_number == first_bill.vendor_invoice_number == "HND-20261015"
    assert [l.billed_before for l in again.lines.all()] == [D("0.00")]
    billed.refresh_from_db()
    assert billed.status == RunStatus.VOID and billed.statement_pdf.read().startswith(b"%PDF")
    assert HandlingRun.objects.filter(period_start=OCT_1.start).count() == 2


# ---- the lock ----

def test_handling_bill_cannot_be_voided_from_bills(billed, admin_user):
    bill = billed.bill
    assert run_lock(bill.journal_entry) == lock_text(bill.number, "Oct 1–15, 2026")
    with pytest.raises((PurchasingError, PostingError)) as e:
        void_bill(bill, user=admin_user, reason="Mistake")
    assert str(e.value) == lock_text(bill.number, "Oct 1–15, 2026")
    bill.refresh_from_db()
    assert bill.status != DocStatus.VOID


def test_handling_bill_cannot_be_edited(billed, admin_user, setup):
    bill = billed.bill
    with pytest.raises((PurchasingError, PostingError)) as e:
        edit_bill(bill, user=admin_user, date=bill.date, vendor_invoice_number=bill.vendor_invoice_number,
                  item_lines=[], expense_lines=[BillExpenseSpec(setup.expense_account, D("20.00"), "Changed")])
    assert str(e.value) == lock_text(bill.number, "Oct 1–15, 2026")
    bill.refresh_from_db()
    (line,) = bill.expense_lines.all()
    assert line.amount == D("9.08") and bill.revision == 1


def test_handling_credit_locked_too(credit_run, admin_user):
    credit = credit_run.vendor_credit
    assert run_lock(credit.journal_entry) == lock_text(credit.number, "Oct 16–31, 2026")
    with pytest.raises((PurchasingError, PostingError)) as e:
        void_vendor_credit(credit, user=admin_user, reason="Mistake")
    assert str(e.value) == lock_text(credit.number, "Oct 16–31, 2026")
    credit.refresh_from_db()
    assert credit.status != DocStatus.VOID

    run = void_run(credit_run, user=admin_user, reason="Redo")
    credit.refresh_from_db()
    assert run.status == RunStatus.VOID
    assert (credit.status, credit.voided_on) == (DocStatus.VOID, d(10, 31))
    assert run_lock(credit.journal_entry) is None


def test_ordinary_bills_unaffected(billed, admin_user, setup, vendor, expense):
    # Even a bill from the handling vendor is ordinary when no run holds it.
    other = create_bill(vendor=setup.vendor, date=d(10, 20), vendor_invoice_number="X-1", user=admin_user,
                        expense_lines=[BillExpenseSpec(setup.expense_account, D("12.00"), "Extra")])
    plain = create_bill(vendor=vendor, date=d(10, 20), vendor_invoice_number="P-1", user=admin_user,
                        expense_lines=[BillExpenseSpec(expense, D("30.00"), "Supplies")])
    assert run_lock(other.journal_entry) is None and run_lock(plain.journal_entry) is None
    edit_bill(plain, user=admin_user, date=d(10, 21), vendor_invoice_number="P-1", item_lines=[],
              expense_lines=[BillExpenseSpec(expense, D("31.00"), "Supplies")])
    plain.refresh_from_db()
    assert plain.revision == 2
    void_bill(plain, user=admin_user, reason="Duplicate")
    void_bill(other, user=admin_user, reason="Duplicate")
    plain.refresh_from_db()
    other.refresh_from_db()
    assert plain.status == other.status == DocStatus.VOID
    # A void run's bill is not locked either (it is already void, so nothing more can happen to it).
    bill = billed.bill
    void_run(billed, user=admin_user, reason="Redo")
    assert run_lock(bill.journal_entry) is None


# ---- month close ----

def test_close_blocked_until_both_periods_billed(setup, admin_user):
    assert close_check(2026, 10) == [
        "October 2026: the handling bill for Oct 1–15, 2026 hasn't been created.",
        "October 2026: the handling bill for Oct 16–31, 2026 hasn't been created.",
    ]
    create_run(OCT_1, user=admin_user, today=LATER)
    assert close_check(2026, 10) == ["October 2026: the handling bill for Oct 16–31, 2026 hasn't been created."]
    with pytest.raises(PeriodCloseBlockedError) as e:
        close_period(2026, 10, admin_user)
    assert "October 2026: the handling bill for Oct 16–31, 2026 hasn't been created." in str(e.value)
    assert "Oct 1–15, 2026" not in str(e.value)

    run = create_run(OCT_2, user=admin_user, today=LATER)
    assert close_check(2026, 10) == []
    # A void run does not count.
    void_run(run, user=admin_user, reason="Redo")
    assert close_check(2026, 10) == ["October 2026: the handling bill for Oct 16–31, 2026 hasn't been created."]
    create_run(OCT_2, user=admin_user, today=LATER)
    period = close_period(2026, 10, admin_user)
    assert period.is_closed and period.close_override_reason == ""


def test_close_override_with_reason(setup, admin_user):
    create_run(OCT_1, user=admin_user, today=LATER)
    period = close_period(2026, 10, admin_user, override_reason="Handling company is late")
    assert period.is_closed
    assert period.close_override_reason == "Handling company is late"
    assert "October 2026: the handling bill for Oct 16–31, 2026 hasn't been created." in (
        period.close_override_problems)


def test_months_before_start_not_checked(setup, admin_user):
    assert close_check(2026, 9) == []
    assert close_check(2025, 12) == []
    assert close_period(2026, 9, admin_user).is_closed
    # A start date inside the second half of the month: the first half can never be billed, so it isn't asked for.
    setup.start_date = d(11, 20)
    setup.save()
    assert close_check(2026, 10) == []
    assert close_check(2026, 11) == ["November 2026: the handling bill for Nov 16–30, 2026 hasn't been created."]


def test_close_check_quiet_when_not_set_up(db):
    assert close_check(2026, 10) == []

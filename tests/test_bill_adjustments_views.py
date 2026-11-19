"""Bill screens with the Adjustment field (adjustments spec, Task 4)."""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from ledger.models import AccountSubtype, AccountType
from purchasing.balances import bill_total
from purchasing.models import Bill, PurchasingSettings
from purchasing.posting.bills import BillAdjustmentSpec, BillExpenseSpec, bill_adjustment, create_bill
from purchasing.posting.orders import POLineSpec, create_po
from tests.conftest import _account
from tests.test_po_bill_views import adm, bill_post, text  # noqa: F401  (fixture)

D = Decimal


@pytest.fixture
def setup(inv_settings, purchasing_settings, sales_settings):
    return purchasing_settings


@pytest.fixture
def wholesale(setup):
    account = _account("5010", "Wholesale Cost of Goods Sold", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
    settings = PurchasingSettings.load()
    settings.default_adjustment_account = account
    settings.save()
    return account


@pytest.fixture
def widget(setup, make_item):
    return make_item("WIDGET", purchase_cost="0.5000", sale_price="2.0000")


def adj(amount, account=None, note=""):
    return {"adj-amount": amount, "adj-account": str(account.pk) if account else "", "adj-note": note}


def test_new_form_preselects_the_default_account(adm, wholesale):
    page = text(adm.get(reverse("purchasing:bill-new")))
    assert "Adjustment account" in page and "Adjustment note" in page
    assert f'<option value="{wholesale.pk}" selected>' in page


def test_new_bill_with_negative_adjustment(adm, vendor, widget, wholesale):
    r = adm.post(reverse("purchasing:bill-new"), bill_post(vendor, ("WIDGET", 100, "0.50"),
                                                           **adj("-2.25", wholesale, "Nova rounding")))
    assert r.status_code == 302
    bill = Bill.objects.get()
    line = bill_adjustment(bill)
    assert (line.amount, line.account, line.description) == (D("-2.25"), wholesale, "Nova rounding")
    assert bill_total(bill) == D("47.75")
    page = text(adm.get(reverse("purchasing:bill-detail", args=[bill.number])))
    assert "Subtotal" in page and "50.00" in page and "-2.25" in page and "47.75" in page and "Nova rounding" in page


def test_blank_or_zero_adjustment_is_none(adm, vendor, widget, wholesale):
    adm.post(reverse("purchasing:bill-new"), bill_post(vendor, ("WIDGET", 10, "0.50"), **adj("0.00", wholesale)))
    bill = Bill.objects.get()
    assert bill_adjustment(bill) is None
    assert "Subtotal" not in text(adm.get(reverse("purchasing:bill-detail", args=[bill.number])))


@pytest.mark.parametrize("amount, message", [
    ("abc", "Adjustment: enter an amount like -10.00 or 25.00."),
    ("1.234", "Adjustment: amounts can have at most 2 decimal places."),
    ("-6.00", "The bill total can't be below zero."),
])
def test_form_errors(adm, vendor, widget, wholesale, amount, message):
    r = adm.post(reverse("purchasing:bill-new"), bill_post(vendor, ("WIDGET", 10, "0.50"), **adj(amount, wholesale)))
    assert r.status_code == 200 and message in text(r)
    assert not Bill.objects.exists()


def test_account_required_and_ap_refused(adm, vendor, widget, setup, ap):
    r = adm.post(reverse("purchasing:bill-new"), bill_post(vendor, ("WIDGET", 10, "0.50"), **adj("-1.00")))
    assert "Adjustment: choose an account." in text(r)
    r = adm.post(reverse("purchasing:bill-new"), bill_post(vendor, ("WIDGET", 10, "0.50"), **adj("-1.00", ap)))
    assert "Adjustment: choose an account other than inventory or accounts payable." in text(r)
    assert not Bill.objects.exists()


def test_edit_loads_and_changes_the_adjustment(adm, vendor, admin_user, wholesale, expense):
    bill = create_bill(vendor=vendor, date=datetime.date(2026, 10, 6), vendor_invoice_number="V-1",
                       user=admin_user, expense_lines=[BillExpenseSpec(expense, D("100.00"), "Freight")],
                       adjustment=BillAdjustmentSpec(wholesale, D("-62.00")))
    page = text(adm.get(reverse("purchasing:bill-edit", args=[bill.number])))
    assert 'name="adj-amount" value="-62.00"' in page
    assert 'name="exp-1-amount" value=""' in page  # the adjustment is not an expense row
    data = bill_post(vendor, expense=[(expense, "100.00", "Freight")], token="tok-edit", reason="Fix credit",
                     **adj("-52.00", wholesale))
    assert adm.post(reverse("purchasing:bill-edit", args=[bill.number]), data).status_code == 302
    bill.refresh_from_db()
    assert bill_adjustment(bill).amount == D("-52.00") and bill_total(bill) == D("48.00")
    assert bill.expense_lines.count() == 2
    rev = text(adm.get(reverse("purchasing:bill-revision", args=[bill.number, 1])))
    assert "-62.00" in rev and "Adjustment" in rev


def test_edit_can_remove_the_adjustment(adm, vendor, admin_user, wholesale, expense):
    bill = create_bill(vendor=vendor, date=datetime.date(2026, 10, 6), vendor_invoice_number="V-1", user=admin_user,
                       expense_lines=[BillExpenseSpec(expense, D("100.00"), "Freight")],
                       adjustment=BillAdjustmentSpec(wholesale, D("5.00")))
    data = bill_post(vendor, expense=[(expense, "100.00", "Freight")], token="tok-edit", **adj("", wholesale))
    adm.post(reverse("purchasing:bill-edit", args=[bill.number]), data)
    assert bill_adjustment(bill) is None and bill_total(bill) == D("100.00")


def test_bill_from_po_with_adjustment(adm, vendor, admin_user, widget, wholesale):
    po = create_po(vendor=vendor, date=datetime.date(2026, 10, 1), user=admin_user, lines=[POLineSpec(widget, 10, D("4.00"))])
    line = po.lines.get()
    data = {"token": "tok-po-bill", "vendor_invoice_number": "N-620562", "date": "2026-10-06", "terms": "", "notes": "",
            f"qty-{line.pk}": "10", f"price-{line.pk}": "4.00", **adj("-3.00", wholesale)}
    assert "Adjustment" in text(adm.get(reverse("purchasing:po-bill", args=[po.number])))
    assert adm.post(reverse("purchasing:po-bill", args=[po.number]), data).status_code == 302
    bill = Bill.objects.get()
    assert bill_total(bill) == D("37.00")
    assert bill.item_lines.get().adjustment_share == D("-3.00")


def test_settings_default_adjustment_account(adm, setup, ap, wholesale):
    page = text(adm.get(reverse("purchasing:settings")))
    assert "Default adjustment account" in page
    assert "Bill adjustments start at this account." in page

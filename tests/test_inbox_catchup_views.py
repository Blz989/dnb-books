import html
import re
from datetime import date
from decimal import Decimal

import pytest
from django.test import Client

from banking.models import BankTransaction, Match, TxnSource, TxnStatus
from items.models import ItemType
from purchasing.models import VendorPayment
from purchasing.posting.bills import BillExpenseSpec, create_bill
from sales.models import Deposit, Payment
from sales.posting.invoices import InvoiceLineSpec, create_invoice

D = Decimal


def d(month, day):
    return date(2026, month, day)


def text(response):
    return html.unescape(response.content.decode())


def token(page, name="token"):
    return re.search(rf'name="{name}" value="([^"]+)"', page).group(1)


@pytest.fixture
def adm(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


@pytest.fixture
def std(standard_user):
    c = Client()
    c.force_login(standard_user)
    return c


@pytest.fixture
def setup(sales_settings, purchasing_settings):
    return None


@pytest.fixture
def service(sales_settings, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def make_txn(checking):
    counter = iter(range(1, 1000))

    def factory(amount, on=d(1, 10), description="Some bank text", check_number=""):
        return BankTransaction.objects.create(
            bank_account=checking, external_id=f"x{next(counter)}", date=on, description=description,
            amount=D(amount), check_number=check_number, source=TxnSource.FILE)

    return factory


@pytest.fixture
def bills(setup, vendor, admin_user, expense):
    return [create_bill(vendor=vendor, date=on, vendor_invoice_number=ref, user=admin_user,
                        expense_lines=[BillExpenseSpec(expense, D(amount))])
            for on, ref, amount in ((d(1, 2), "V-1", "300.00"), (d(1, 5), "V-2", "200.00"), (d(1, 8), "V-3", "100.00"))]


@pytest.fixture
def invoices(setup, customer, admin_user, service):
    return [create_invoice(customer=customer, date=on, user=admin_user, lines=[InvoiceLineSpec(service, 1, D(amount))])
            for on, amount in ((d(1, 2), "400.00"), (d(1, 6), "250.00"))]


def pay_url(txn):
    return f"/banking/inbox/{txn.pk}/pay-bills/"


def receive_url(txn):
    return f"/banking/inbox/{txn.pk}/receive/"


def test_pay_bills_flow(adm, bills, vendor, make_txn):
    txn = make_txn("-450.00")
    empty = text(adm.get(pay_url(txn)))
    assert "Choose a vendor" in empty and "V-1" not in empty
    listed = text(adm.get(pay_url(txn), {"vendor": vendor.pk}))
    assert listed.index("V-1") < listed.index("V-2") < listed.index("V-3")
    filled = text(adm.get(pay_url(txn), {"vendor": vendor.pk, "fill": "1"}))
    assert 'value="300.00"' in filled and 'value="150.00"' in filled
    assert ">450.00</span> of 450.00" in filled
    r = adm.post(pay_url(txn), {"token": token(filled), "vendor": vendor.pk,
                                f"amt-{bills[0].pk}": "300.00", f"amt-{bills[1].pk}": "150.00"}, follow=True)
    assert r.status_code == 200
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    assert VendorPayment.objects.get().amount == D("450.00")
    assert Match.objects.filter(bank_transaction=txn).exists()


def test_receive_payment_flow_check(adm, invoices, customer, make_txn):
    txn = make_txn("650.00", check_number="1042")
    page = text(adm.get(receive_url(txn), {"customer": customer.pk, "fill": "1"}))
    assert '<option value="CHECK" selected>' in page
    adm.post(receive_url(txn), {"token": token(page), "customer": customer.pk, "method": "CHECK",
                                f"amt-{invoices[0].pk}": "400.00", f"amt-{invoices[1].pk}": "250.00"})
    txn.refresh_from_db()
    assert txn.status == TxnStatus.MATCHED
    payment = Payment.objects.get()
    assert payment.reference == "1042" and payment.amount == D("650.00")
    deposit = Deposit.objects.get()
    assert list(deposit.payments.all()) == [payment]
    assert Match.objects.get(bank_transaction=txn).deposit == deposit


def test_mismatch_keeps_inputs(adm, bills, vendor, make_txn):
    txn = make_txn("-450.00")
    page = text(adm.get(pay_url(txn), {"vendor": vendor.pk}))
    r = adm.post(pay_url(txn), {"token": token(page), "vendor": vendor.pk, f"amt-{bills[0].pk}": "300.00"})
    body = text(r)
    assert r.status_code == 200
    assert "The bills you chose add up to 300.00. The bank shows 450.00." in body
    assert 'class="banner error"' in body and 'value="300.00"' in body
    assert ">300.00</span> of 450.00" in body
    txn.refresh_from_db()
    assert txn.status == TxnStatus.NEW
    # the token was not spent by the failure
    ok = adm.post(pay_url(txn), {"token": token(body), "vendor": vendor.pk, f"amt-{bills[0].pk}": "300.00",
                                 f"amt-{bills[1].pk}": "150.00"})
    assert ok.status_code == 302


def test_double_post_same_token_one_payment(adm, bills, vendor, make_txn):
    txn = make_txn("-450.00")
    page = text(adm.get(pay_url(txn), {"vendor": vendor.pk, "fill": "1"}))
    data = {"token": token(page), "vendor": vendor.pk, f"amt-{bills[0].pk}": "300.00", f"amt-{bills[1].pk}": "150.00"}
    adm.post(pay_url(txn), data)
    r = adm.post(pay_url(txn), data, follow=True)
    assert "That was already done." in text(r)
    assert VendorPayment.objects.count() == 1


def test_actions_shown_by_sign(adm, make_txn):
    out = make_txn("-20.00", description="OUTGOING ROW")
    money_in = make_txn("30.00", description="INCOMING ROW")
    page = text(adm.get("/banking/inbox/"))
    assert pay_url(out) in page and receive_url(out) not in page
    assert receive_url(money_in) in page and pay_url(money_in) not in page


def test_standard_user_can_use_both(std, bills, invoices, vendor, customer, make_txn):
    out = make_txn("-300.00")
    page = text(std.get(pay_url(out), {"vendor": vendor.pk, "fill": "1"}))
    std.post(pay_url(out), {"token": token(page), "vendor": vendor.pk, f"amt-{bills[0].pk}": "300.00"})
    out.refresh_from_db()
    assert out.status == TxnStatus.MATCHED
    money_in = make_txn("400.00")
    page = text(std.get(receive_url(money_in), {"customer": customer.pk, "fill": "1"}))
    std.post(receive_url(money_in), {"token": token(page), "customer": customer.pk, "method": "ACH",
                                     f"amt-{invoices[0].pk}": "400.00"})
    money_in.refresh_from_db()
    assert money_in.status == TxnStatus.MATCHED


def test_login_required_and_wrong_sign_refused(adm, client, make_txn):
    out = make_txn("-20.00")
    assert client.get(pay_url(out)).status_code == 302
    assert adm.get(receive_url(out)).status_code == 302


def test_method_survives_fill(adm, invoices, customer, make_txn):
    txn = make_txn("400.00")
    page = text(adm.get(receive_url(txn), {"customer": customer.pk, "method": "WIRE", "fill": "1"}))
    assert '<option value="WIRE" selected>' in page
    assert 'name="method" value="WIRE" id="pick-method"' in page


def test_non_ascii_party_id_is_not_a_500(adm, make_txn):
    out = make_txn("-20.00")
    money_in = make_txn("30.00")
    assert adm.get(pay_url(out), {"vendor": "\u00b2"}).status_code == 200
    assert adm.get(receive_url(money_in), {"customer": "\u00b2"}).status_code == 200
    assert adm.post(pay_url(out), {"token": "t", "vendor": "\u00b2"}).status_code == 200

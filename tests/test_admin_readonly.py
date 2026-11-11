from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from banking.models import (
    AccountKind, BankAccount, BankTransaction, Rule, Transfer, TxnSource,
)
from purchasing.models import (
    Bill, PurchaseOrder, PurchasingSettings, Threshold1099, VendorCredit, VendorCreditKind, VendorPayment,
    VendorPaymentMethod,
)

D = Decimal
DAY = date(2026, 1, 10)

READ_ONLY = ["bill", "vendorpayment", "vendorcredit", "purchaseorder", "banktransaction", "transfer"]


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser("root", "root@example.com", "pw-123456")


@pytest.fixture
def adm(superuser):
    c = Client()
    c.force_login(superuser)
    return c


@pytest.fixture
def rows(vendor, cash, checking, superuser):
    savings = BankAccount.objects.create(
        ledger_account=cash.__class__.objects.create(number="1010", name="Savings", type=cash.type,
                                                      subtype=cash.subtype),
        kind=AccountKind.SAVINGS, mask="9999")
    return {
        "bill": Bill.objects.create(number="BILL-00001", vendor=vendor, date=DAY, due_date=DAY,
                                    vendor_invoice_number="V-1", created_by=superuser),
        "vendorpayment": VendorPayment.objects.create(
            number="VP-00001", vendor=vendor, date=DAY, method=VendorPaymentMethod.ACH, paid_from=cash,
            amount=D("5.00"), created_by=superuser),
        "vendorcredit": VendorCredit.objects.create(
            number="VC-00001", vendor=vendor, date=DAY, kind=VendorCreditKind.ALLOWANCE, created_by=superuser),
        "purchaseorder": PurchaseOrder.objects.create(number="PO-00001", vendor=vendor, date=DAY, created_by=superuser),
        "banktransaction": BankTransaction.objects.create(
            bank_account=checking, external_id="x1", date=DAY, description="x", amount=D("1.00"),
            source=TxnSource.FILE),
        "transfer": Transfer.objects.create(date=DAY, from_account=checking, to_account=savings, amount=D("2.00"),
                                            created_by=superuser),
    }


def url(app, model, name, *args):
    return reverse(f"admin:{app}_{model}_{name}", args=args)


APP = {"bill": "purchasing", "vendorpayment": "purchasing", "vendorcredit": "purchasing",
       "purchaseorder": "purchasing", "banktransaction": "banking", "transfer": "banking"}


@pytest.mark.parametrize("model", READ_ONLY)
def test_documents_can_be_viewed_but_not_changed(adm, rows, model):
    app, obj = APP[model], rows[model]
    assert adm.get(url(app, model, "changelist")).status_code == 200
    detail = adm.get(url(app, model, "change", obj.pk))
    assert detail.status_code == 200
    body = detail.content.decode()
    assert 'name="_save"' not in body and "deletelink" not in body
    assert adm.get(url(app, model, "add")).status_code == 403
    assert adm.post(url(app, model, "add"), {}).status_code == 403
    assert adm.post(url(app, model, "change", obj.pk), {"number": "HACKED"}).status_code == 403
    assert adm.get(url(app, model, "delete", obj.pk)).status_code == 403
    assert adm.post(url(app, model, "delete", obj.pk), {"post": "yes"}).status_code == 403
    assert type(obj).objects.filter(pk=obj.pk).exists()


def test_settings_thresholds_accounts_and_rules_stay_editable(adm, checking, expense):
    rule = Rule.objects.create(pattern="fee", account=expense)
    threshold = Threshold1099.objects.get_or_create(year=2026, defaults={"amount": D("600.00")})[0]
    targets = [("purchasing", "purchasingsettings", PurchasingSettings.load()),
               ("purchasing", "threshold1099", threshold), ("banking", "bankaccount", checking),
               ("banking", "rule", rule)]
    for app, model, obj in targets:
        detail = adm.get(url(app, model, "change", obj.pk))
        assert detail.status_code == 200 and 'name="_save"' in detail.content.decode(), model
        assert adm.get(url(app, model, "add")).status_code == 200, model

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied

from items.models import ItemType
from ledger.cash_basis import profit_and_loss_cash
from ledger.models import Account, AccountSubtype, AccountType
from sales.balances import invoice_balance
from sales.errors import ReceiptError
from sales.models import CreditNote
from sales.posting.invoices import InvoiceLineSpec, create_invoice, void_invoice
from sales.posting.receipts import (
    apply_credit, issue_refund, receive_payment, remove_application, void_credit_note,
)
from sales.posting.writeoff import WriteOffError, write_off
from sales.reports import ar_aging

D = Decimal


@pytest.fixture
def bad_debt(db):
    return Account.objects.create(number="6900", name="Bad debt", type=AccountType.EXPENSE, subtype=AccountSubtype.EXPENSE)


@pytest.fixture
def setup(inv_settings, sales_settings, bad_debt):
    sales_settings.bad_debt_account = bad_debt
    sales_settings.save()
    return sales_settings


@pytest.fixture
def svc(setup, make_item):
    return make_item("SVC", item_type=ItemType.NON_STOCK, costing_method="", cogs_account=None, inventory_account=None)


@pytest.fixture
def inv(svc, customer, admin_user):
    return create_invoice(customer=customer, date=date(2026, 3, 1), user=admin_user,
                          lines=[InvoiceLineSpec(svc, 1, D("500.00"))])


def ledger_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


def test_write_off_full_balance(inv, admin_user, bad_debt, ar):
    note = write_off(inv, date=date(2026, 6, 30), amount=D("500.00"), reason="Customer closed", user=admin_user)
    assert note.write_off_of == inv and note.customer == inv.customer
    assert note.reason == "Bad debt: Customer closed"
    assert ledger_lines(note.journal_entry) == [("1200", D("0.00"), D("500.00")), ("6900", D("500.00"), D("0.00"))]
    assert invoice_balance(inv) == D("0.00")
    app = note.applications.get()
    assert (app.invoice, app.amount, app.applied_on) == (inv, D("500.00"), date(2026, 6, 30))
    assert ar_aging(date(2026, 7, 1)).totals.total == D("0.00")


def test_write_off_part_after_a_payment(inv, admin_user, cash):
    receive_payment(customer=inv.customer, date=date(2026, 4, 1), amount=D("200.00"), method="ACH",
                    bank_account=cash, user=admin_user, applications=[(inv, D("200.00"))])
    write_off(inv, date=date(2026, 6, 30), amount=D("300.00"), reason="Settled for less", user=admin_user)
    assert invoice_balance(inv) == D("0.00")


@pytest.mark.parametrize("kwargs,message", [
    ({"reason": "  "}, "Enter a reason."),
    ({"amount": D("0.00")}, "Enter an amount above zero."),
    ({"amount": D("500.01")}, "INV-000001 has 500.00 left to pay."),
    ({"date": date(2026, 2, 28)}, "The write-off date can't be before the invoice date."),
])
def test_refusals(inv, admin_user, kwargs, message):
    args = {"date": date(2026, 6, 30), "amount": D("100.00"), "reason": "Gone", **kwargs}
    with pytest.raises((WriteOffError, ReceiptError)) as exc:
        write_off(inv, user=admin_user, **args)
    assert str(exc.value) == message.replace("INV-000001", inv.number)
    assert not CreditNote.objects.exists()


def test_admin_only(inv, standard_user):
    with pytest.raises(PermissionDenied):
        write_off(inv, date=date(2026, 6, 30), amount=D("1.00"), reason="x", user=standard_user)


def test_void_invoice_refused(inv, admin_user):
    void_invoice(inv, user=admin_user, reason="Wrong")
    with pytest.raises(WriteOffError, match=f"{inv.number} is void and can't be written off."):
        write_off(inv, date=date(2026, 6, 30), amount=D("1.00"), reason="x", user=admin_user)


def test_setting_missing(inv, admin_user, setup):
    setup.bad_debt_account = None
    setup.save()
    with pytest.raises(WriteOffError, match="Set the bad debt account in Settings, Sales first."):
        write_off(inv, date=date(2026, 6, 30), amount=D("1.00"), reason="x", user=admin_user)


def test_no_refund_and_no_other_invoice(inv, admin_user, svc, cash):
    note = write_off(inv, date=date(2026, 6, 30), amount=D("100.00"), reason="Partial", user=admin_user)
    app = note.applications.get()
    remove_application(app, date=date(2026, 6, 30), user=admin_user)
    with pytest.raises(ReceiptError, match="A write-off can't be refunded."):
        issue_refund(source=note, date=date(2026, 7, 1), bank_account=cash, method="CHECK", amount=D("10.00"),
                     user=admin_user)
    other = create_invoice(customer=inv.customer, date=date(2026, 3, 2), user=admin_user,
                           lines=[InvoiceLineSpec(svc, 1, D("50.00"))])
    with pytest.raises(ReceiptError, match="A write-off applies only to the invoice it was made for."):
        apply_credit(note, other, D("10.00"), date=date(2026, 7, 1), user=admin_user)
    apply_credit(note, inv, D("100.00"), date=date(2026, 7, 1), user=admin_user)  # back on its own invoice


def test_undo_by_removing_then_voiding(inv, admin_user):
    note = write_off(inv, date=date(2026, 6, 30), amount=D("500.00"), reason="Gone", user=admin_user)
    remove_application(note.applications.get(), date=date(2026, 7, 2), user=admin_user)
    void_credit_note(note, user=admin_user, reason="They paid after all", date=date(2026, 7, 2))
    assert invoice_balance(inv) == D("500.00")


def test_cash_basis_counts_nothing(inv, admin_user, revenue, bad_debt):
    write_off(inv, date=date(2026, 6, 30), amount=D("500.00"), reason="Gone", user=admin_user)
    pnl = profit_and_loss_cash(date(2026, 1, 1), date(2026, 12, 31))
    numbers = {r.account.number for r in [*pnl.income, *pnl.operating_expenses]}
    assert "4000" not in numbers and "6900" not in numbers
    assert pnl.net_income == D("0.00")


def test_ordinary_credit_note_cash_basis_unchanged(inv, admin_user, cash):
    from sales.posting.receipts import CreditLineSpec, create_credit_note

    note = create_credit_note(customer=inv.customer, date=date(2026, 5, 1), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged", D("100.00"))])
    apply_credit(note, inv, D("100.00"), date=date(2026, 5, 1), user=admin_user)
    receive_payment(customer=inv.customer, date=date(2026, 5, 2), amount=D("400.00"), method="ACH",
                    bank_account=cash, user=admin_user, applications=[(inv, D("400.00"))])
    assert profit_and_loss_cash(date(2026, 1, 1), date(2026, 12, 31)).net_income == D("400.00")


def test_sales_by_customer_shows_written_off(inv, admin_user):
    from ledger.sales_reports import sales_by_customer

    write_off(inv, date=date(2026, 3, 30), amount=D("500.00"), reason="Gone", user=admin_user)
    r = sales_by_customer(date(2026, 3, 1), date(2026, 3, 31))
    row = r.rows[0]
    assert (row.sales, row.credits, row.net, row.written_off) == (D("500.00"), D("0.00"), D("500.00"), D("500.00"))
    assert r.difference == D("0.00")


def test_write_off_lowers_order_profit(inv, admin_user):
    from handling.profit import order_profit

    before = order_profit(inv.number, date(2026, 12, 31))
    write_off(inv, date=date(2026, 6, 30), amount=D("200.00"), reason="Partial", user=admin_user)
    after = order_profit(inv.number, date(2026, 12, 31))
    assert after.credits == before.credits + D("200.00")
    assert after.profit == before.profit - D("200.00")


# ---- screens ----

def test_invoice_page_button_and_form(client, inv, admin_user, standard_user):
    client.force_login(standard_user)
    assert "Write off" not in client.get(f"/sales/invoices/{inv.number}/").content.decode()
    client.force_login(admin_user)
    assert "Write off" in client.get(f"/sales/invoices/{inv.number}/").content.decode()
    url = f"/sales/invoices/{inv.number}/write-off/"
    html = client.get(url).content.decode()
    assert "500.00" in html
    html = client.post(url, {"date": "2026-06-30", "amount": "500.00", "reason": ""}).content.decode()
    assert "Enter a reason." in html
    resp = client.post(url, {"date": "2026-06-30", "amount": "500.00", "reason": "Closed"})
    assert resp.status_code == 302
    note = CreditNote.objects.get()
    html = client.get(f"/sales/credits/{note.number}/").content.decode()
    assert f"Write-off of {inv.number}" in html


def test_form_needs_setting(client, inv, admin_user, setup):
    setup.bad_debt_account = None
    setup.save()
    client.force_login(admin_user)
    html = client.get(f"/sales/invoices/{inv.number}/write-off/").content.decode()
    assert "Set the bad debt account in Settings, Sales first." in html


def test_standard_user_cannot_open_form(client, inv, standard_user):
    client.force_login(standard_user)
    assert client.get(f"/sales/invoices/{inv.number}/write-off/").status_code == 403


def test_sales_settings_field(client, admin_user, setup):
    client.force_login(admin_user)
    assert "Bad debt account" in client.get("/sales/settings/").content.decode()

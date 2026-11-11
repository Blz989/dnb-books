"""Task 1 and 2 of the review fixes: a document's entry or movement can only be voided through the document, and
the close check that reports a document whose entry or movement was voided anyway."""
from datetime import date
from decimal import Decimal

import pytest

from banking.models import AccountKind, BankAccount, TransferKind
from banking.transfers import create_transfer, void_transfer
from items.errors import ItemStateError
from items.models import MovementKind, StockMovement
from items.stock import StockLine, record_movements, void_movement
from ledger.errors import EntryStateError
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus, JournalEntry
from ledger.posting import LineSpec, post_entry, void_entry
from purchasing.models import VendorPaymentMethod
from purchasing.posting.bills import BillExpenseSpec, BillItemSpec, create_bill, edit_bill, void_bill
from purchasing.posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, create_allowance_credit, create_return_credit, void_vendor_credit,
)
from purchasing.posting.payments import pay_vendor, void_vendor_payment
from sales.models import DocStatus, PaymentMethod
from sales.posting.deposits import make_deposit, void_deposit
from sales.posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, void_invoice
from sales.posting.receipts import (
    CreditLineSpec, create_credit_note, issue_refund, receive_payment, void_credit_note, void_payment, void_refund,
)

from tests.test_handling_locks import billed, gadget, media, setup  # noqa: E402,F401  (handling fixtures)

D = Decimal


def d(month, day):
    return date(2026, month, day)


@pytest.fixture
def books(inv_settings, sales_settings, purchasing_settings, bank, expense):
    return bank


@pytest.fixture
def bolt(books, make_item, admin_user):
    item = make_item("BOLT", sale_price="2.0000", purchase_cost="0.5000")
    record_movements(kind=MovementKind.OPENING, date=d(10, 1), user=admin_user,
                     lines=[StockLine(item, 20, unit_cost=D("0.50"))])
    return item


def entry_msg(noun, number):
    return f"This entry belongs to {noun} {number}. Void the {noun} instead."


def movement_msg(noun, number):
    return f"This movement belongs to {noun} {number}. Void or edit the {noun} instead."


def refused(entry, admin_user, noun):
    with pytest.raises(EntryStateError) as exc:
        void_entry(entry, user=admin_user, reason="oops")
    assert str(exc.value) == entry_msg(noun, entry.source_id)
    entry.refresh_from_db()
    assert entry.status == EntryStatus.POSTED


def paid_invoice(customer, bolt, admin_user, bank):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    return invoice


# ---- the nine document sources ----

def test_invoice_entry_locked_and_invoice_void_works(bolt, customer, admin_user):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    refused(invoice.journal_entry, admin_user, "invoice")
    void_invoice(invoice, user=admin_user, reason="Wrong customer")
    invoice.refresh_from_db()
    assert invoice.status == DocStatus.VOID


def test_customer_payment_entry_locked(books, customer, admin_user, bank):
    payment = receive_payment(customer=customer, date=d(10, 6), method=PaymentMethod.ACH, amount=D("10.00"),
                              user=admin_user, bank_account=bank)
    refused(payment.journal_entry, admin_user, "customer payment")
    void_payment(payment, user=admin_user, reason="Bounced")
    payment.refresh_from_db()
    assert payment.status == DocStatus.VOID


def test_credit_note_entry_locked(books, customer, admin_user):
    note = create_credit_note(customer=customer, date=d(10, 6), user=admin_user, reason="Damaged",
                              lines=[CreditLineSpec("Damaged goods", D("5.00"))])
    refused(note.journal_entry, admin_user, "credit note")
    void_credit_note(note, user=admin_user, reason="Mistake")
    note.refresh_from_db()
    assert note.status == DocStatus.VOID


def test_refund_entry_locked(books, customer, admin_user, bank):
    payment = receive_payment(customer=customer, date=d(10, 6), method=PaymentMethod.ACH, amount=D("10.00"),
                              user=admin_user, bank_account=bank)
    refund = issue_refund(source=payment, date=d(10, 7), bank_account=bank, method="ACH", amount=D("4.00"),
                          user=admin_user)
    refused(refund.journal_entry, admin_user, "refund")
    void_refund(refund, user=admin_user, reason="Mistake")
    refund.refresh_from_db()
    assert refund.status == DocStatus.VOID


def test_deposit_entry_locked(books, customer, admin_user, bank):
    payment = receive_payment(customer=customer, date=d(10, 6), method=PaymentMethod.CHECK, amount=D("10.00"),
                              user=admin_user)
    deposit = make_deposit(payments=[payment], bank_account=bank, date=d(10, 7), user=admin_user)
    refused(deposit.journal_entry, admin_user, "deposit")
    void_deposit(deposit, user=admin_user, reason="Mistake")
    deposit.refresh_from_db()
    assert deposit.status == DocStatus.VOID


@pytest.fixture
def stock_bill(books, bolt, vendor, admin_user, expense):
    return create_bill(vendor=vendor, date=d(10, 5), vendor_invoice_number="V-1", user=admin_user,
                       item_lines=[BillItemSpec(bolt, 10, D("0.50"))],
                       expense_lines=[BillExpenseSpec(expense, D("3.00"), "Freight")])


def test_bill_entry_locked(stock_bill, admin_user):
    refused(stock_bill.journal_entry, admin_user, "bill")
    void_bill(stock_bill, user=admin_user, reason="Wrong vendor", allow_negative=True)
    stock_bill.refresh_from_db()
    assert stock_bill.status == DocStatus.VOID


def test_vendor_payment_entry_locked(books, vendor, admin_user, bank):
    payment = pay_vendor(vendor=vendor, date=d(10, 6), method=VendorPaymentMethod.ACH, amount=D("10.00"),
                         paid_from=bank, user=admin_user)
    refused(payment.journal_entry, admin_user, "vendor payment")
    void_vendor_payment(payment, user=admin_user, reason="Mistake")
    payment.refresh_from_db()
    assert payment.status == DocStatus.VOID


def test_vendor_credit_entry_locked(books, vendor, admin_user, expense):
    credit = create_allowance_credit(vendor=vendor, date=d(10, 6), user=admin_user, reason="Allowance",
                                     lines=[AllowanceLineSpec(expense, D("5.00"), "Allowance")])
    refused(credit.journal_entry, admin_user, "vendor credit")
    void_vendor_credit(credit, user=admin_user, reason="Mistake")
    credit.refresh_from_db()
    assert credit.status == DocStatus.VOID


@pytest.fixture
def savings(db):
    ledger = Account.objects.create(number="1010", name="Savings", type=AccountType.ASSET, subtype=AccountSubtype.BANK)
    return BankAccount.objects.create(ledger_account=ledger, kind=AccountKind.SAVINGS, mask="4321")


def test_transfer_entry_locked(books, checking, savings, admin_user):
    transfer = create_transfer(date=d(10, 6), from_account=checking, to_account=savings, amount=D("50.00"),
                               kind=TransferKind.TRANSFER, user=admin_user)
    with pytest.raises(EntryStateError) as exc:
        void_entry(transfer.journal_entry, user=admin_user, reason="oops")
    assert str(exc.value) == entry_msg("transfer", f"TR-{transfer.pk}")
    void_transfer(transfer, user=admin_user, reason="Mistake")
    transfer.refresh_from_db()
    assert transfer.status == "VOID"


# ---- not locked ----

def test_manual_entry_can_still_be_voided(books, admin_user, bank, revenue):
    entry = post_entry(date=d(10, 5), user=admin_user,
                       lines=[LineSpec(bank, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    void_entry(entry, user=admin_user, reason="Typo")
    entry.refresh_from_db()
    assert entry.status == EntryStatus.VOID


def test_banking_entry_source_can_still_be_voided(books, admin_user, bank, revenue):
    entry = post_entry(date=d(10, 5), user=admin_user, source_type="banking.entry", source_id="BT-1",
                       lines=[LineSpec(bank, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    void_entry(entry, user=admin_user, reason="Back to the inbox")
    entry.refresh_from_db()
    assert entry.status == EntryStatus.VOID


# ---- movements ----

def test_invoice_movement_locked(bolt, customer, admin_user):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    movement = invoice.lines.get().stock_movement
    with pytest.raises(ItemStateError) as exc:
        void_movement(movement, user=admin_user, reason="oops")
    assert str(exc.value) == movement_msg("invoice", invoice.number)
    movement.refresh_from_db()
    assert not movement.voided


def test_bill_movement_locked(stock_bill, admin_user):
    movement = StockMovement.objects.get(source_type="purchasing.bill")
    with pytest.raises(ItemStateError) as exc:
        void_movement(movement, user=admin_user, reason="oops", allow_negative=True)
    assert str(exc.value) == movement_msg("bill", stock_bill.number)


def test_vendor_return_movement_locked(bolt, vendor, admin_user):
    credit = create_return_credit(vendor=vendor, date=d(10, 6), user=admin_user, reason="Defective",
                                  lines=[ReturnLineSpec(bolt, 2)])
    movement = StockMovement.objects.get(source_type="purchasing.vendor_credit")
    with pytest.raises(ItemStateError) as exc:
        void_movement(movement, user=admin_user, reason="oops")
    assert str(exc.value) == movement_msg("vendor credit", credit.number)
    void_vendor_credit(credit, user=admin_user, reason="Mistake")
    movement.refresh_from_db()
    assert movement.voided


def test_non_document_movements_can_still_be_voided(bolt, admin_user):
    adjustment, = record_movements(kind=MovementKind.ADJUSTMENT, date=d(10, 5), user=admin_user,
                                   lines=[StockLine(bolt, 1, "Found one", unit_cost=D("0.50"))])
    void_movement(adjustment, user=admin_user, reason="Typo")
    adjustment.refresh_from_db()
    assert adjustment.voided


def test_zoho_style_adjustment_can_still_be_voided(bolt, admin_user):
    adjustment, = record_movements(kind=MovementKind.ADJUSTMENT, date=d(10, 5), user=admin_user,
                                   lines=[StockLine(bolt, 1, "Free goods", unit_cost=D("0.50"))],
                                   source_type="migration.free_goods", source_id="Z-1")
    void_movement(adjustment, user=admin_user, reason="Typo")
    adjustment.refresh_from_db()
    assert adjustment.voided


# ---- document services keep working ----

def test_standard_user_can_edit_invoice_and_bill(bolt, stock_bill, customer, admin_user, standard_user, expense):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    out = edit_invoice(invoice, user=standard_user, date=invoice.date, terms=invoice.terms, ship_to="", notes="x",
                       lines=[InvoiceLineSpec(bolt, 3, D("2.00"))])
    assert out.revision == 2
    bill = edit_bill(stock_bill, user=standard_user, date=stock_bill.date, vendor_invoice_number="V-1",
                     item_lines=[BillItemSpec(bolt, 8, D("0.50"))],
                     expense_lines=[BillExpenseSpec(expense, D("3.00"), "Freight")], allow_negative=True)
    assert bill.revision == 2


# ---- screens ----

@pytest.fixture
def admin_client(admin_user):
    from django.test import Client

    c = Client()
    c.force_login(admin_user)
    return c


def test_journal_page_shows_message_instead_of_void_button(bolt, customer, admin_user, admin_client):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    html = admin_client.get(f"/books/journal/{invoice.journal_entry.pk}/").content.decode()
    assert entry_msg("invoice", invoice.number) in html
    assert "/void/" not in html


def test_journal_page_keeps_void_button_for_manual_entry(books, admin_user, bank, revenue, admin_client):
    entry = post_entry(date=d(10, 5), user=admin_user,
                       lines=[LineSpec(bank, debit=D("5.00")), LineSpec(revenue, credit=D("5.00"))])
    html = admin_client.get(f"/books/journal/{entry.pk}/").content.decode()
    assert f"/books/journal/{entry.pk}/void/" in html


def test_journal_void_post_for_document_entry_shows_message(bolt, customer, admin_user, admin_client):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    r = admin_client.post(f"/books/journal/{invoice.journal_entry.pk}/void/",
                          {"date": "2026-10-06", "reason": "oops"})
    assert r.status_code == 200 and entry_msg("invoice", invoice.number) in r.content.decode()


def test_item_page_hides_void_link_for_document_movements_only(bolt, customer, admin_user, admin_client):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    adjustment, = record_movements(kind=MovementKind.ADJUSTMENT, date=d(10, 6), user=admin_user,
                                   lines=[StockLine(bolt, 1, "Found", unit_cost=D("0.50"))])
    sale = invoice.lines.get().stock_movement
    html = admin_client.get("/items/BOLT/").content.decode()
    assert f"/items/movements/{adjustment.pk}/void/" in html
    assert f"/items/movements/{sale.pk}/void/" not in html
    assert f"invoice {invoice.number}" in html


def test_void_movement_page_post_for_document_movement_shows_message(bolt, customer, admin_user, admin_client):
    invoice = create_invoice(customer=customer, date=d(10, 5), user=admin_user,
                             lines=[InvoiceLineSpec(bolt, 4, D("2.00"))])
    sale = invoice.lines.get().stock_movement
    r = admin_client.post(f"/items/movements/{sale.pk}/void/", {"date": "2026-10-06", "reason": "oops"})
    assert movement_msg("invoice", invoice.number) in r.content.decode()


# ---- handling ----

def test_handling_bill_still_gives_the_handling_message(billed, admin_user):
    with pytest.raises(EntryStateError) as exc:
        void_entry(billed.bill.journal_entry, user=admin_user, reason="oops")
    assert str(exc.value) == f"{billed.bill.number} is the handling bill for {billed.label}. Void it from Handling."


def test_void_handling_run_still_works_through_the_bill_service(billed, admin_user):
    from handling.billing import void_run

    void_run(billed, user=admin_user, reason="Wrong rate")
    billed.bill.refresh_from_db()
    assert billed.bill.status == DocStatus.VOID

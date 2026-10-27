"""Demo sales data for seed_demo: customers, orders, invoices (one edited, one voided, one drop-ship line),
payments by check, cash, and ACH, a deposit, a credit note, and a refund."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contacts.models import Customer, Terms
from items.models import Item
from ledger.models import Account

from .models import Fulfillment, PaymentMethod, RefundMethod, SalesSettings
from .posting.deposits import make_deposit
from .posting.invoices import InvoiceLineSpec, create_invoice, edit_invoice, invoice_from_order, void_invoice
from .posting.orders import OrderLineSpec, create_order
from .posting.receipts import (
    CreditLineSpec, apply_credit, create_credit_note, issue_refund, receive_payment,
)

D = Decimal

CUSTOMERS = [
    ("Acme Builders", "Dana Whitfield", "dana@acmebuilders.example", "555-0142", Terms.NET_30,
     "100 Main St\nSpringfield, IL 62701", "Dock 4, 200 Industrial Way\nSpringfield, IL 62702"),
    ("Harbor Plumbing", "Luis Ortega", "luis@harborplumbing.example", "555-0177", Terms.NET_15,
     "44 Harbor Rd\nPort Clyde, ME 04855", "44 Harbor Rd\nPort Clyde, ME 04855"),
    ("Oak & Pine Remodeling", "Mia Chen", "mia@oakpine.example", "555-0120", Terms.NET_45,
     "9 Orchard Ln\nBurlington, VT 05401", "Job site: 71 Elm St\nBurlington, VT 05401"),
    ("Riverside School District", "Accounts Payable", "ap@riversideschools.example", "555-0199", Terms.NET_30,
     "500 School Way\nRiverside, CA 92501", "Maintenance yard, 12 Depot St\nRiverside, CA 92503"),
    ("Walk-in customer", "", "", "", Terms.DUE_ON_RECEIPT, "", ""),
]


def seed_sales(user, items) -> None:
    by_number = {a.number: a for a in Account.objects.all()}
    s = SalesSettings.load()
    s.ar_account = by_number["1200"]
    s.undeposited_account = by_number["1250"]
    s.returns_account = by_number["4900"]
    s.company_name = "DNB Supply Co."
    s.company_address = "1450 Commerce Drive\nSpringfield, IL 62704"
    s.company_phone = "555-0100"
    s.company_email = "billing@dnbsupply.example"
    s.default_invoice_notes = "Thank you for your business. Please include the invoice number with your payment."
    s.save()

    c = {}
    for name, contact, email, phone, terms, billing, ship in CUSTOMERS:
        c[name] = Customer.objects.create(
            name=name, contact_name=contact, email=email, phone=phone, terms=terms,
            billing_address=billing, ship_to_address=ship)
    bank = by_number["1000"]
    today = timezone.localdate()

    def day(n):
        return today - timedelta(days=n)

    item = lambda sku: Item.objects.get(sku=sku)  # noqa: E731
    drop = dict(fulfillment=Fulfillment.DROP_SHIP, drop_ship_vendor="Delta Faucet Supply")

    # Acme: one order shipped in two parts, the second including a drop-shipped faucet.
    so1 = create_order(customer=c["Acme Builders"], date=day(44), user=user, lines=[
        OrderLineSpec(item("BOLT-14"), 200, D("0.89")),
        OrderLineSpec(item("PNT-WHT-GL"), 12, D("32.99")),
        OrderLineSpec(item("FAUCET-KIT"), 2, D("69.00"), **drop),
    ])
    bolts, paint, faucets = so1.lines.all()
    inv1 = invoice_from_order(so1, date=day(40), quantities={bolts.pk: 200, paint.pk: 6, faucets.pk: 0}, user=user)
    invoice_from_order(so1, date=day(12), quantities={paint.pk: 6, faucets.pk: 2}, user=user)

    # Harbor: a partly shipped order, then a corrected quantity on its invoice.
    so2 = create_order(customer=c["Harbor Plumbing"], date=day(27), user=user, lines=[
        OrderLineSpec(item("PIPE-PVC-12"), 60, D("4.29")),
        OrderLineSpec(item("VALVE-BALL-12"), 24, D("11.49")),
        OrderLineSpec(item("DELIVERY"), 1, D("45.00")),
    ])
    pipe, valve, delivery = so2.lines.all()
    inv3 = invoice_from_order(so2, date=day(25), quantities={pipe.pk: 60, valve.pk: 10, delivery.pk: 1}, user=user)
    edit_invoice(
        inv3, user=user, date=inv3.date, terms=inv3.terms, ship_to=inv3.ship_to, notes=inv3.notes,
        reason="Customer took 12 valves, not 10",
        lines=[
            InvoiceLineSpec(item("PIPE-PVC-12"), 60, D("4.29"), so_line=pipe),
            InvoiceLineSpec(item("VALVE-BALL-12"), 12, D("11.49"), so_line=valve),
            InvoiceLineSpec(item("DELIVERY"), 1, D("45.00"), so_line=delivery),
        ])

    inv4 = create_invoice(customer=c["Riverside School District"], date=day(68), user=user, lines=[
        InvoiceLineSpec(item("GLOVE-NIT-L"), 10, D("17.99")),
        InvoiceLineSpec(item("TAPE-DUCT"), 24, D("7.99")),
        InvoiceLineSpec(item("TARP-8X10"), 6, D("9.99")),
    ])
    create_invoice(customer=c["Riverside School District"], date=day(45), user=user, lines=[
        InvoiceLineSpec(item("ZIP-14"), 12, D("8.49")),
        InvoiceLineSpec(item("GLOVE-NIT-M"), 4, D("17.99")),
    ])
    create_invoice(customer=c["Oak & Pine Remodeling"], date=day(5), user=user, lines=[
        InvoiceLineSpec(item("WIRE-12-250"), 3, D("139.00")),
        InvoiceLineSpec(item("OUTLET-15"), 100, D("2.29")),
    ])
    mistake = create_invoice(customer=c["Oak & Pine Remodeling"], date=day(15), user=user, lines=[
        InvoiceLineSpec(item("BREAK-20"), 8, D("11.99"))])
    void_invoice(mistake, user=user, reason="Entered for the wrong customer")
    inv6 = create_invoice(customer=c["Walk-in customer"], date=day(2), user=user, lines=[
        InvoiceLineSpec(item("TAPE-PTFE"), 5, D("1.69")), InvoiceLineSpec(item("CAULK-CLR"), 3, D("6.99"))])

    # Payments: a check that gets deposited, an ACH overpayment, a part-payment by check, and cash.
    from .balances import invoice_balance

    check = receive_payment(
        customer=c["Acme Builders"], date=day(30), method=PaymentMethod.CHECK, amount=invoice_balance(inv1),
        reference="4410", user=user, applications=[(inv1, invoice_balance(inv1))])
    make_deposit(payments=[check], bank_account=bank, date=day(28), user=user)

    note = create_credit_note(
        customer=c["Harbor Plumbing"], date=day(14), user=user, reason="Two valves arrived damaged",
        lines=[CreditLineSpec("Damaged ball valves, 2 at 11.49", D("22.98"))])
    apply_credit(note, inv3, D("22.98"), date=day(13), user=user)
    ach = receive_payment(
        customer=c["Harbor Plumbing"], date=day(10), method=PaymentMethod.ACH, amount=D("450.00"),
        reference="ACH 88213", bank_account=bank, user=user, applications=[(inv3, invoice_balance(inv3))])
    issue_refund(source=ach, date=day(6), bank_account=bank, method=RefundMethod.ACH, amount=D("20.00"),
                 reference="Refund of overpayment", user=user)

    receive_payment(
        customer=c["Riverside School District"], date=day(20), method=PaymentMethod.CHECK, amount=D("200.00"),
        reference="5102", user=user, applications=[(inv4, D("200.00"))])
    receive_payment(
        customer=c["Walk-in customer"], date=day(2), method=PaymentMethod.CASH, amount=invoice_balance(inv6),
        user=user, applications=[(inv6, invoice_balance(inv6))])

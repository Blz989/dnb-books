"""Demo purchasing data for seed_demo: vendors, purchase orders received in parts, a drop-ship PO and its bill,
expense bills, payments by check, ACH, and card, a return credit, an allowance, and one edited bill."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from contacts.models import Terms, Vendor
from items.models import Item
from ledger.models import Account
from sales.models import SalesOrder

from .models import PurchasingSettings, VendorPaymentMethod as M
from .posting.bills import BillExpenseSpec, BillItemSpec, bill_from_po, create_bill, edit_bill
from .posting.credits import (
    AllowanceLineSpec, ReturnLineSpec, apply_vendor_credit, create_allowance_credit, create_return_credit,
)
from .posting.orders import POLineSpec, create_drop_ship_pos, create_po
from .posting.payments import PayLine, pay_bills

D = Decimal


def seed_purchasing(user, items) -> None:
    by_number = {a.number: a for a in Account.objects.all()}
    settings = PurchasingSettings.load()
    settings.ap_account = by_number["2000"]
    settings.save()
    bank, card, freight, rent = by_number["1000"], by_number["2100"], by_number["6150"], by_number["6000"]
    today = timezone.localdate()

    def day(n):
        return today - timedelta(days=n)

    item = lambda sku: Item.objects.get(sku=sku)  # noqa: E731
    midwest = Vendor.objects.create(
        name="Midwest Fastener Co", contact_name="Pat Novak", email="orders@midwestfastener.example",
        phone="555-0311", address="800 Industrial Pkwy\nToledo, OH 43604", terms=Terms.NET_30)
    delta = Vendor.objects.create(
        name="Delta Faucet Supply", contact_name="Rina Shah", email="sales@deltafaucet.example",
        phone="555-0388", address="12 Commerce Way\nIndianapolis, IN 46204", terms=Terms.NET_30)
    landlord = Vendor.objects.create(
        name="Springfield Properties", contact_name="Accounts", email="rent@springfieldprops.example",
        phone="555-0144", address="1 Court St\nSpringfield, IL 62701", terms=Terms.NET_30,
        default_expense_account=rent)
    rivera = Vendor.objects.create(
        name="Rivera Delivery LLC", contact_name="Marco Rivera", email="marco@riveradelivery.example",
        phone="555-0266", address="77 Depot Rd\nSpringfield, IL 62703", terms=Terms.NET_15, is_1099=True,
        tax_id="47-1234567", default_expense_account=freight)

    # Stock from Midwest, received in two bills would leave nothing open, so one PO is billed in part.
    po1 = create_po(vendor=midwest, date=day(40), user=user, expected_date=day(30), lines=[
        POLineSpec(item("BOLT-14"), 1000, D("0.52")), POLineSpec(item("NUT-14"), 1000, D("0.13"))])
    bolts, nuts = po1.lines.all()
    bill1 = bill_from_po(
        po1, date=day(36), vendor_invoice_number="MF-20418", quantities={bolts.pk: 600, nuts.pk: 600}, user=user,
        expense_lines=[BillExpenseSpec(freight, D("48.50"), "Freight")])
    create_po(vendor=midwest, date=day(4), user=user, expected_date=today + timedelta(days=10), lines=[
        POLineSpec(item("BOLT-38"), 500, D("0.74")), POLineSpec(item("ANCH-38"), 200, D("0.96"))])

    # Drop-ship: the faucets on the Acme order are bought from Delta and shipped straight to the customer.
    order = SalesOrder.objects.filter(lines__fulfillment="DROP_SHIP").distinct().first()
    if order is not None:
        (po_drop,) = create_drop_ship_pos(order, user=user, date=day(11))
        bill_from_po(po_drop, date=day(9), vendor_invoice_number="DF-77120",
                     quantities={l.pk: l.quantity for l in po_drop.lines.all()}, user=user)

    # Expense bills: rent twice, and delivery services.
    rent1 = create_bill(vendor=landlord, date=day(55), vendor_invoice_number="RENT-SEP", user=user,
                        item_lines=[], expense_lines=[BillExpenseSpec(rent, D("2400.00"), "September rent")])
    create_bill(vendor=landlord, date=day(25), vendor_invoice_number="RENT-OCT", user=user,
                expense_lines=[BillExpenseSpec(rent, D("2400.00"), "October rent")])
    r1 = create_bill(vendor=rivera, date=day(80), vendor_invoice_number="RD-1031", user=user,
                     expense_lines=[BillExpenseSpec(freight, D("800.00"), "Deliveries, August")])
    r2 = create_bill(vendor=rivera, date=day(40), vendor_invoice_number="RD-1090", user=user,
                     expense_lines=[BillExpenseSpec(freight, D("1450.00"), "Deliveries, September")])
    r3 = create_bill(vendor=rivera, date=day(6), vendor_invoice_number="RD-1144", user=user,
                     expense_lines=[BillExpenseSpec(freight, D("650.00"), "Deliveries, October")])

    # One check, two ACH payments (one partial), and one company card payment.
    pay_bills(date=day(30), method=M.CHECK, paid_from=bank, user=user, lines=[PayLine(rent1, D("2400.00"))],
              check_numbers={landlord.pk: "3301"})
    pay_bills(date=day(70), method=M.ACH, paid_from=bank, user=user, lines=[PayLine(r1, D("800.00"))])
    pay_bills(date=day(30), method=M.ACH, paid_from=bank, user=user, lines=[PayLine(r2, D("1450.00"))])
    pay_bills(date=day(20), method=M.ACH, paid_from=bank, user=user, lines=[PayLine(bill1, D("300.00"))])
    pay_bills(date=day(3), method=M.CARD, paid_from=card, user=user, lines=[PayLine(r3, D("650.00"))])

    # Ten bolts go back to Midwest for 5.20, applied to their bill. Delta gives an unapplied allowance.
    credit = create_return_credit(vendor=midwest, date=day(14), user=user, reason="Wrong plating",
                                  lines=[ReturnLineSpec(item("BOLT-14"), 10, D("5.20"))])
    apply_vendor_credit(credit, bill1, D("5.20"), date=day(13), user=user)
    create_allowance_credit(vendor=delta, date=day(8), user=user, reason="Late shipment",
                            lines=[AllowanceLineSpec(freight, D("15.00"), "Late delivery allowance")])

    # The Midwest invoice showed 52.00 for freight, not 48.50.
    edit_bill(bill1, user=user, date=bill1.date, vendor_invoice_number=bill1.vendor_invoice_number,
              reason="Freight on the invoice was 52.00",
              item_lines=[BillItemSpec(l.item, l.quantity, l.price, l.description, l.po_line)
                          for l in bill1.item_lines.all()],
              expense_lines=[BillExpenseSpec(freight, D("52.00"), "Freight")])

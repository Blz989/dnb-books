"""Demo handling setup for seed_demo: the handling company, its expense account, a start date, and the 55% rate."""
from decimal import Decimal

from django.utils import timezone

from contacts.models import Terms, Vendor
from ledger.models import Account, AccountSubtype, AccountType
from sales.models import Invoice, SalesOrder

from .models import HandlingSettings
from .rates import add_rate


def seed_handling(user) -> None:
    vendor = Vendor.objects.create(
        name="Harbor Handling LLC", contact_name="Sam Okafor", email="billing@harborhandling.example",
        phone="555-0412", address="3 Pier Rd\nSpringfield, IL 62702", terms=Terms.NET_30)
    account, _ = Account.objects.get_or_create(
        number="6400", defaults={"name": "Handling", "type": AccountType.EXPENSE, "subtype": AccountSubtype.EXPENSE})
    # An order's date is its sales order's date, or a direct invoice's date.
    dates = [d for d in (
        SalesOrder.objects.order_by("date").values_list("date", flat=True).first(),
        Invoice.objects.filter(sales_order__isnull=True).order_by("date").values_list("date", flat=True).first(),
    ) if d is not None]
    start = min(dates, default=timezone.localdate()).replace(day=1)
    s = HandlingSettings.load()
    s.vendor, s.expense_account, s.start_date = vendor, account, start
    s.save()
    add_rate(effective_from=start, percent=Decimal("55"), user=user)

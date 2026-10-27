from django.core.management import call_command

from sales.checks import ar_check, undeposited_check
from sales.models import DocStatus, Invoice, Payment, Refund


def test_seed_demo_sales_tie_out_to_the_ledger(db):
    call_command("seed_demo", verbosity=0)
    assert ar_check() == [] and undeposited_check() == []
    assert Invoice.objects.filter(status=DocStatus.VOID).count() == 1
    assert Invoice.objects.filter(revision__gt=1).count() == 1
    assert Payment.objects.count() == 4 and Refund.objects.count() == 1


def test_seed_demo_purchasing_ties_out_to_the_ledger(db):
    from items.stock import check_invariants
    from purchasing.checks import ap_check
    from purchasing.models import Bill, DropShipAllocation, VendorCredit, VendorPayment
    from sales.checks import ar_check, undeposited_check

    call_command("seed_demo", verbosity=0)
    assert ap_check() == [] and ar_check() == [] and undeposited_check() == [] and check_invariants() == []
    assert Bill.objects.count() >= 6 and VendorPayment.objects.count() == 5 and VendorCredit.objects.count() == 2
    assert DropShipAllocation.objects.exists()

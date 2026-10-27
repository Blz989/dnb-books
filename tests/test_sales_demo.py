from django.core.management import call_command

from sales.checks import ar_check, undeposited_check
from sales.models import DocStatus, Invoice, Payment, Refund


def test_seed_demo_sales_tie_out_to_the_ledger(db):
    call_command("seed_demo", verbosity=0)
    assert ar_check() == [] and undeposited_check() == []
    assert Invoice.objects.filter(status=DocStatus.VOID).count() == 1
    assert Invoice.objects.filter(revision__gt=1).count() == 1
    assert Payment.objects.count() == 4 and Refund.objects.count() == 1

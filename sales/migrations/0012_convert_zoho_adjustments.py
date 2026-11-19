"""Zoho invoice adjustments were imported as a line on the ADJ item (or an ADJ-<account> copy). Flag each as the
invoice's adjustment, only when the invoice has exactly one. Flags only: no amount and no journal line changes."""
from django.db import migrations
from django.db.models import Count, Q

ADJ = Q(item__sku="ADJ") | Q(item__sku__startswith="ADJ-")


def convert(apps, schema_editor=None):
    Line = apps.get_model("sales", "InvoiceLine")
    single = Line.objects.filter(ADJ).values("invoice_id").annotate(n=Count("id")).filter(n=1).values("invoice_id")
    Line.objects.filter(ADJ, invoice_id__in=single).update(is_adjustment=True)


def unconvert(apps, schema_editor=None):
    apps.get_model("sales", "InvoiceLine").objects.filter(is_adjustment=True).update(is_adjustment=False)


class Migration(migrations.Migration):
    dependencies = [("sales", "0011_adjustments"), ("items", "0001_initial")]
    operations = [migrations.RunPython(convert, unconvert)]

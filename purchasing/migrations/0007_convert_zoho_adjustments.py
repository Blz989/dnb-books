"""Zoho bill adjustments were imported as an expense line described "Adjustment". Flag each as the bill's
adjustment (only when the bill has exactly one), and seed the default adjustment account. Flags only: no amount
and no journal line changes. Converted adjustments don't spread into drop-ship order cost."""
from django.db import migrations
from django.db.models import Count


def convert(apps, schema_editor=None):
    Line = apps.get_model("purchasing", "BillExpenseLine")
    single = (Line.objects.filter(description="Adjustment").values("bill_id")
              .annotate(n=Count("id")).filter(n=1).values("bill_id"))
    Line.objects.filter(description="Adjustment", bill_id__in=single).update(
        is_adjustment=True, spreads_to_orders=False)

    Settings = apps.get_model("purchasing", "PurchasingSettings")
    Account = apps.get_model("ledger", "Account")
    account = Account.objects.filter(number="5010").first()
    if account is not None:
        settings, _ = Settings.objects.get_or_create(pk=1)
        if settings.default_adjustment_account_id is None:
            settings.default_adjustment_account = account
            settings.save(update_fields=["default_adjustment_account"])


def unconvert(apps, schema_editor=None):
    apps.get_model("purchasing", "BillExpenseLine").objects.filter(is_adjustment=True).update(
        is_adjustment=False, spreads_to_orders=False)


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0006_adjustments"), ("ledger", "0003_close_override")]
    operations = [migrations.RunPython(convert, unconvert)]

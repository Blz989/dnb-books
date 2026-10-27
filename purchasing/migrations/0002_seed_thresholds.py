from decimal import Decimal

from django.db import migrations

THRESHOLDS = [(2025, Decimal("600.00")), (2026, Decimal("2000.00"))]


def seed(apps, schema_editor):
    Threshold = apps.get_model("purchasing", "Threshold1099")
    for year, amount in THRESHOLDS:
        Threshold.objects.get_or_create(year=year, defaults={"amount": amount})


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

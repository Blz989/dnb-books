from django.db import migrations

SEQUENCES = [
    ("SO", "SO-", 5),
    ("INV", "INV-", 6),
    ("PMT", "PMT-", 5),
    ("CN", "CN-", 5),
    ("DEP", "DEP-", 5),
    ("REF", "REF-", 5),
]


def seed(apps, schema_editor):
    Sequence = apps.get_model("sales", "DocumentSequence")
    for doc_type, prefix, padding in SEQUENCES:
        Sequence.objects.get_or_create(
            doc_type=doc_type, defaults={"prefix": prefix, "padding": padding, "next_number": 1}
        )


class Migration(migrations.Migration):
    dependencies = [("sales", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]

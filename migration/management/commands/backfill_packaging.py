"""Fill Packaging on items that are already in the database, from a Zoho Item.csv (matched by SKU)."""
import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from items.models import Item

COLUMNS = ("CF.Packaging", "Packaging")


class Command(BaseCommand):
    help = "Set Packaging on existing items from a Zoho Item.csv. Only touches the packaging note."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=Path)

    def handle(self, *args, csv_path, **options):
        if not csv_path.is_file():
            raise CommandError(f"{csv_path} is not a file.")
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            column = next((c for c in COLUMNS if c in (reader.fieldnames or [])), None)
            if column is None:
                raise CommandError('The file has no "CF.Packaging" or "Packaging" column.')
            rows = [(r.get("SKU", "").strip(), (r.get(column) or "").strip()) for r in reader]
        items = {i.sku.lower(): i for i in Item.objects.all()}
        changed = 0
        for sku, packaging in rows:
            item = items.get(sku.lower())
            if item and packaging and item.packaging != packaging:
                item.packaging = packaging
                item.save(update_fields=["packaging"])
                changed += 1
        self.stdout.write(f"Packaging set on {changed} item{'s' if changed != 1 else ''}.")

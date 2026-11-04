"""SKU series: offer the next SKU for a new item, and keep the counter ahead of SKUs that use the series."""
import re

from django.db.models.functions import Lower

from .models import Item, InventorySettings

MAX_TRIES = 1000


def format_sku(prefix: str, number: int, digits: int) -> str:
    return f"{prefix}{number:0{digits}d}"


def next_sku(settings: InventorySettings | None = None) -> str:
    """The next unused SKU in the series, or "" when no series is set up."""
    s = settings or InventorySettings.load()
    if not s.sku_prefix:
        return ""
    taken = set(Item.objects.filter(sku__istartswith=s.sku_prefix).annotate(l=Lower("sku")).values_list("l", flat=True))
    n = s.sku_next
    for _ in range(MAX_TRIES):
        candidate = format_sku(s.sku_prefix, n, s.sku_digits)
        if candidate.lower() not in taken:
            return candidate
        n += 1
    return ""


def advance_past(sku: str) -> None:
    """After an item is saved: if its SKU is in the series at or beyond the next number, move the counter past it."""
    s = InventorySettings.load()
    if not s.sku_prefix:
        return
    m = re.fullmatch(re.escape(s.sku_prefix) + r"(\d+)", sku, flags=re.IGNORECASE)
    if m and int(m.group(1)) >= s.sku_next:
        s.sku_next = int(m.group(1)) + 1
        s.save(update_fields=["sku_next"])

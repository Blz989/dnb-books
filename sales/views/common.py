"""Shared by the sales screens: line parsing, one-time form tokens, and error context."""
import uuid
from decimal import Decimal, InvalidOperation

from django.http import QueryDict

from items.errors import SettingsMissingError
from items.models import Item
from items.views import _claim_token, _token_used  # noqa: F401  (the Phase 2 one-time token helpers)

from ..errors import SalesSettingsMissing
from ..models import Fulfillment

MAX_LINES = 30
MIN_SHOWN = 5


def new_token() -> str:
    return str(uuid.uuid4())


def blank_row(i: int) -> dict:
    return {"index": i, "sku": "", "quantity": "", "price": "", "description": "", "fulfillment": Fulfillment.STOCK,
            "vendor": "", "ref": ""}


def read_rows(post) -> list[dict]:
    rows = []
    for i in range(MAX_LINES):
        rows.append({
            "index": i, "sku": post.get(f"lines-{i}-sku", "").strip(), "quantity": post.get(f"lines-{i}-quantity", "").strip(),
            "price": post.get(f"lines-{i}-price", "").strip(), "description": post.get(f"lines-{i}-description", "").strip(),
            "fulfillment": post.get(f"lines-{i}-fulfillment", Fulfillment.STOCK), "vendor": post.get(f"lines-{i}-vendor", "").strip(),
            "ref": post.get(f"lines-{i}-ref", "").strip(),
        })
    return rows


def is_blank(row: dict) -> bool:
    return not row["sku"] and not row["quantity"]


def compact(rows: list[dict]) -> list[dict]:
    """Filled rows first, so the line numbers in error messages match what the user sees."""
    filled = [r for r in rows if not is_blank(r)]
    rows = filled + [blank_row(0)] * (MAX_LINES - len(filled))
    return [{**r, "index": i} for i, r in enumerate(rows)]


def price_text(price: Decimal) -> str:
    """12.5000 shows as 12.50; 0.8350 keeps its fourth place only when needed."""
    text = f"{price:f}"
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0").ljust(2, "0")
    return f"{whole}.{frac}"


def rows_from_lines(lines, ref=lambda line: line.pk) -> list[dict]:
    """Rows for the form, from saved order or invoice lines."""
    rows = []
    for i, line in enumerate(lines):
        rows.append({
            "index": i, "sku": line.item.sku, "quantity": str(line.quantity), "price": price_text(line.price),
            "description": line.description, "fulfillment": line.fulfillment, "vendor": line.drop_ship_vendor,
            "ref": str(ref(line) or ""),
        })
    return compact(rows)


def shown_count(rows: list[dict]) -> int:
    last = max([r["index"] for r in rows if not is_blank(r)] + [-1])
    return min(max(last + 2, MIN_SHOWN), MAX_LINES)


def parse_rows(rows: list[dict]):
    """Returns (parsed, errors). Each parsed entry: item, quantity, price, description, fulfillment, vendor, ref."""
    parsed, errors, n = [], [], 0
    for row in rows:
        if is_blank(row):
            continue
        n += 1
        item = Item.objects.filter(sku__iexact=row["sku"]).first() if row["sku"] else None
        if not row["sku"]:
            errors.append(f"Line {n}: choose an item.")
            continue
        if item is None:
            errors.append(f"Line {n}: no item has SKU {row['sku']}.")
            continue
        try:
            quantity = int(row["quantity"])
        except ValueError:
            errors.append(f"Line {n}: enter a quantity above zero.")
            continue
        if row["price"]:
            try:
                price = Decimal(row["price"].replace(",", ""))
                if not price.is_finite():
                    raise InvalidOperation
            except InvalidOperation:
                errors.append(f"Line {n}: enter a price like 12.50.")
                continue
        else:
            price = item.sale_price
        fulfillment = row["fulfillment"] if row["fulfillment"] in Fulfillment.values else None
        if fulfillment is None:
            errors.append(f"Line {n}: choose where it ships from.")
            continue
        parsed.append({
            "item": item, "quantity": quantity, "price": price, "description": row["description"],
            "fulfillment": fulfillment, "vendor": row["vendor"], "ref": row["ref"],
        })
    return parsed, errors


def error_context(exc) -> dict:
    link = None
    if isinstance(exc, SalesSettingsMissing):
        link = "/sales/settings/"
    elif isinstance(exc, SettingsMissingError):
        link = "/items/settings/"
    return {"errors": [str(exc)], "settings_link": link}


def hidden_fields(post: QueryDict) -> list[tuple[str, str]]:
    """Everything posted, so a confirm page can submit the same form again."""
    skip = {"csrfmiddlewaretoken", "allow_negative", "back"}
    return [(key, value) for key, values in post.lists() if key not in skip for value in values]

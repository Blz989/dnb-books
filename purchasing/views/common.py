"""Shared by the purchase order and bill screens: row parsing, form tokens, confirm pages."""
from decimal import Decimal, InvalidOperation

from django.http import QueryDict

from items.errors import SettingsMissingError
from items.models import Item
from ledger.models import Account, AccountType
from sales.views.common import (  # noqa: F401  (shared row helpers and one-time form tokens)
    MAX_LINES, MIN_SHOWN, _claim_token, _token_used, compact, is_blank, new_token, price_text, read_rows, shown_count,
)

from ..errors import PurchasingSettingsMissing


def safe_id(value) -> int | None:
    """A database id from query-string text, or None. Long digit strings would overflow SQLite's integers."""
    value = str(value or "")
    return int(value) if value.isdigit() and len(value) <= 12 else None


def error_context(exc) -> dict:
    link = None
    if isinstance(exc, PurchasingSettingsMissing):
        link = "/purchasing/settings/"
    elif isinstance(exc, SettingsMissingError):
        link = "/items/settings/"
    return {"errors": [str(exc)], "settings_link": link}


def parse_item_rows(rows: list[dict]):
    """Returns (parsed, errors). Each parsed entry: item, quantity, price, description, ref."""
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
            price = item.purchase_cost
        parsed.append({
            "item": item, "quantity": quantity, "price": price, "description": row["description"], "ref": row["ref"],
        })
    return parsed, errors


def item_rows_from_lines(lines, ref=lambda line: line.pk) -> list[dict]:
    rows = []
    for i, line in enumerate(lines):
        rows.append({
            "index": i, "sku": line.item.sku, "quantity": str(line.quantity), "price": price_text(line.price),
            "description": line.description, "fulfillment": "", "vendor": "", "ref": str(ref(line) or ""),
        })
    return compact(rows)


# ---- expense rows ----

def blank_expense(i: int) -> dict:
    return {"index": i, "account": "", "amount": "", "description": ""}


def read_expense_rows(post) -> list[dict]:
    return [
        {"index": i, "account": post.get(f"exp-{i}-account", "").strip(), "amount": post.get(f"exp-{i}-amount", "").strip(),
         "description": post.get(f"exp-{i}-description", "").strip()}
        for i in range(MAX_LINES)
    ]


def expense_is_blank(row: dict) -> bool:
    return not row["account"] and not row["amount"]


def compact_expense(rows: list[dict]) -> list[dict]:
    filled = [r for r in rows if not expense_is_blank(r)]
    rows = filled + [blank_expense(0)] * (MAX_LINES - len(filled))
    return [{**r, "index": i} for i, r in enumerate(rows)]


def expense_rows_from_lines(lines) -> list[dict]:
    return compact_expense([
        {"index": i, "account": str(l.account_id), "amount": f"{l.amount:.2f}", "description": l.description}
        for i, l in enumerate(lines)
    ])


def expense_shown(rows: list[dict]) -> int:
    last = max([r["index"] for r in rows if not expense_is_blank(r)] + [-1])
    return min(max(last + 2, 3), MAX_LINES)


def parse_expense_rows(rows: list[dict], start: int = 0):
    """Line numbers continue after the item lines, as the service numbers them."""
    parsed, errors, n = [], [], start
    for row in rows:
        if expense_is_blank(row):
            continue
        n += 1
        account = Account.objects.filter(pk=safe_id(row["account"]), type=AccountType.EXPENSE, is_active=True).first() \
            if safe_id(row["account"]) else None
        if account is None:
            errors.append(f"Line {n}: choose an account.")
            continue
        try:
            amount = Decimal(row["amount"].replace(",", ""))
            if not amount.is_finite() or amount <= 0:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"Line {n}: enter an amount above zero.")
            continue
        parsed.append({"account": account, "amount": amount, "description": row["description"]})
    return parsed, errors


def expense_accounts():
    return Account.objects.filter(type=AccountType.EXPENSE, is_active=True).order_by("number")


def sku_options():
    return Item.objects.filter(is_active=True).order_by("sku").values("sku", "name")[:1000]


def hidden_fields(post: QueryDict, drop: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """Everything posted, so a confirm page can submit the same form again. `drop` names the flag the confirm
    button sets itself."""
    skip = {"csrfmiddlewaretoken", "back", *drop}
    return [(key, value) for key, values in post.lists() if key not in skip for value in values]

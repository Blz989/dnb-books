"""Pure mapping functions from Zoho Books export values to DNB Books values."""

import re
from datetime import date
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from banking.models import AccountKind
from contacts.models import Terms
from ledger.models import AccountSubtype, AccountType
from purchasing.models import VendorPaymentMethod
from sales.models import PaymentMethod

CENT = Decimal("0.01")
PRICE_STEP = Decimal("0.0001")
UNDEPOSITED = "Undeposited Funds"
PETTY_CASH = "Petty Cash"

# Zoho type -> (type, subtype, first number, last number, fixed number or None)
_A, _L, _S = AccountType, AccountType.LIABILITY, AccountSubtype
ACCOUNT_RULES = {
    "Bank": (_A.ASSET, _S.BANK, 1000, 1099, None),
    "Cash": (_A.ASSET, _S.BANK, 1000, 1099, None),
    "Accounts Receivable": (_A.ASSET, _S.ACCOUNTS_RECEIVABLE, 1100, 1100, 1100),
    "Other Current Asset": (_A.ASSET, _S.OTHER_CURRENT_ASSET, 1200, 1299, None),
    "Payment Clearing": (_A.ASSET, _S.OTHER_CURRENT_ASSET, 1200, 1299, None),
    "Stock": (_A.ASSET, _S.INVENTORY, 1300, 1399, None),
    "Fixed Asset": (_A.ASSET, _S.FIXED_ASSET, 1500, 1599, None),
    "Accounts Payable": (_L, _S.ACCOUNTS_PAYABLE, 2000, 2000, 2000),
    "Credit Card": (_L, _S.CREDIT_CARD, 2100, 2199, None),
    "Other Current Liability": (_L, _S.OTHER_CURRENT_LIABILITY, 2200, 2399, None),
    "Other Liability": (_L, _S.LONG_TERM_LIABILITY, 2500, 2599, None),
    "Equity": (AccountType.EQUITY, _S.EQUITY, 3000, 3099, None),
    "Income": (AccountType.INCOME, _S.INCOME, 4000, 4099, None),
    "Cost Of Goods Sold": (AccountType.EXPENSE, _S.COST_OF_GOODS_SOLD, 5000, 5099, None),
    "Expense": (AccountType.EXPENSE, _S.EXPENSE, 6000, 7999, None),
    "Other Expense": (AccountType.EXPENSE, _S.OTHER_EXPENSE, 8000, 8099, None),
}
# Number ranges that share one pool (Bank and Cash both use 1000-1099, and so on).
_POOLS = {
    "Bank": "Bank", "Cash": "Bank",
    "Other Current Asset": "OCA", "Payment Clearing": "OCA",
}


@dataclass(frozen=True)
class AccountPlan:
    name: str
    number: str
    type: AccountType
    subtype: AccountSubtype
    is_active: bool


def clean_sku(text: str) -> str:
    """A Zoho SKU made to fit DNB Books' SKU rule: "&" becomes "and", any other character outside letters,
    numbers, dots, dashes, and underscores becomes "-", and it is cut to 50 characters."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip().replace("&", "and")).strip("-._")
    return cleaned[:50].rstrip("-._")


def money(text: str) -> Decimal:
    """Zoho amounts: "1,234.5", "0.000", and on items "USD 2.99". Only US dollars are supported."""
    cleaned = (text or "").replace(",", "").strip()
    currency = re.match(r"^([A-Za-z]{3})\s+", cleaned)
    if currency:
        if currency.group(1).upper() != "USD":
            raise ValueError(f"Only US dollar amounts can be imported, not '{text}'.")
        cleaned = cleaned[currency.end():]
    if not cleaned:
        return Decimal("0.00")
    return Decimal(cleaned).quantize(CENT, rounding=ROUND_HALF_UP)


def account_plan(rows: list[dict]) -> list[AccountPlan]:
    pools: dict[str, list[dict]] = {}
    for row in rows:
        ztype = row["Account Type"].strip()
        if ztype not in ACCOUNT_RULES:
            raise ValueError(f"Unknown Zoho account type '{ztype}' for account {row['Account Name']}.")
        pools.setdefault(_POOLS.get(ztype, ztype), []).append(row)
    plans = []
    for members in pools.values():
        first_type = members[0]["Account Type"].strip()
        _, _, low, high, fixed = ACCOUNT_RULES[first_type]
        members = sorted(members, key=lambda r: (r["Account Name"].casefold(), r["Account Name"]))
        for i, row in enumerate(members):
            number = fixed if fixed is not None and i == 0 else low + 10 * i
            if number > high or (fixed is not None and i > 0):
                raise ValueError(
                    f"Too many Zoho '{first_type}' accounts for the number range {low}-{high}."
                )
            atype, subtype, *_ = ACCOUNT_RULES[row["Account Type"].strip()]
            plans.append(AccountPlan(
                name=row["Account Name"], number=str(number), type=atype, subtype=subtype,
                is_active=row.get("Account Status", "Active").strip().lower() != "inactive",
            ))
    return plans


def bank_kind(name: str, zoho_type: str) -> AccountKind | None:
    is_loc = name.startswith("Line Of Credit")
    if zoho_type == "Bank":
        return AccountKind.CHECKING
    if zoho_type == "Cash":
        return None if name in (UNDEPOSITED, PETTY_CASH) else AccountKind.CHECKING
    if zoho_type == "Credit Card":
        return AccountKind.LOAN if is_loc else AccountKind.CARD
    if zoho_type == "Other Liability" and is_loc:
        return AccountKind.LOAN
    return None


def terms(label: str, issued: str = "", due: str = "") -> Terms:
    """Zoho's terms label as DNB Books terms. "Custom" terms carry only a due date, so their length is the gap
    between the document date and the due date (Net 30 when either date is missing)."""
    text = (label or "").strip()
    if not text:
        return Terms.NET_30
    lowered = text.lower()
    if lowered == "due on receipt":
        return Terms.DUE_ON_RECEIPT
    if lowered == "due end of the month":
        return Terms.NET_30
    if lowered == "custom":
        try:
            days = (date.fromisoformat(due.strip()) - date.fromisoformat(issued.strip())).days
        except ValueError:
            return Terms.NET_30
        if days <= 0:
            return Terms.DUE_ON_RECEIPT
        return _net_terms(days)
    match = re.fullmatch(r"net\s+(\d+)", lowered)
    if not match:
        raise ValueError(f"Unknown Zoho payment terms '{label}'.")
    return _net_terms(int(match.group(1)))


def _net_terms(days: int) -> Terms:
    options = sorted((int(t.value.split("_")[1]), t) for t in Terms if t.value.startswith("NET_"))
    for option_days, term in options:
        if option_days >= days:
            return term
    return options[-1][1]


def address(row: dict, prefix: str) -> str:
    def get(key):
        return (row.get(f"{prefix} {key}") or "").strip()

    city_line = ", ".join(p for p in (get("City"), get("State")) if p)
    city_line = " ".join(p for p in (city_line, get("Code")) if p)
    return "\n".join(p for p in (get("Address"), get("Street2"), city_line) if p)


def line_price(total: Decimal, quantity: int) -> tuple[Decimal, Decimal]:
    if not quantity:
        return Decimal("0.0000"), total
    price = (total / quantity).quantize(PRICE_STEP, rounding=ROUND_HALF_UP)
    extended = (price * quantity).quantize(CENT, rounding=ROUND_HALF_UP)
    return price, total - extended


def customer_method(mode: str, deposit_to: str) -> PaymentMethod:
    if deposit_to.strip() == UNDEPOSITED:
        return PaymentMethod.CHECK if mode.strip() == "Check" else PaymentMethod.CASH
    return PaymentMethod.ACH


def vendor_method(mode: str, paid_through_subtype) -> VendorPaymentMethod:
    if paid_through_subtype == AccountSubtype.CREDIT_CARD:
        return VendorPaymentMethod.CARD
    if mode.strip() == "Check":
        return VendorPaymentMethod.CHECK
    return VendorPaymentMethod.ACH

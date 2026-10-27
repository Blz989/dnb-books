class PurchasingError(Exception):
    """Base for every problem the purchasing services report. Messages are plain language."""


class PurchasingSettingsMissing(PurchasingError):
    pass


class UnresolvedVendors(PurchasingError):
    """Drop-ship vendor names that match no Vendor. `unresolved` holds the names as typed."""

    def __init__(self, unresolved: list[str]):
        self.unresolved = unresolved
        super().__init__(f"No vendor named {unresolved[0]}. Choose a vendor for these lines.")


# Ledger amounts are 14 digits with 2 decimals; stay well inside them so a typo is a message, not a crash.
MAX_QUANTITY = 10_000_000
MAX_PRICE_TEXT = "100000000"
MAX_AMOUNT_TEXT = "999999999999.99"

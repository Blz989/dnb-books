class PurchasingError(Exception):
    """Base for every problem the purchasing services report. Messages are plain language."""


class PurchasingSettingsMissing(PurchasingError):
    pass


class UnresolvedVendors(PurchasingError):
    """Drop-ship vendor names that match no Vendor. `unresolved` holds the names as typed."""

    def __init__(self, unresolved: list[str]):
        self.unresolved = unresolved
        super().__init__(f"No vendor named {unresolved[0]}. Choose a vendor for these lines.")

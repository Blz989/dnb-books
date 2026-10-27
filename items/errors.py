class StockError(Exception):
    """Base for every problem the stock service reports. Messages are plain language."""


class SettingsMissingError(StockError):
    pass


class NegativeStockWarning(StockError):
    """Raised before saving when stock would go below zero and the caller has not confirmed."""

    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__(" ".join(messages))


class CostingError(StockError):
    pass


class ItemStateError(StockError):
    pass


class CountError(StockError):
    pass


class CountNeedsConfirmation(StockError):
    def __init__(self, skus: list[str]):
        self.skus = skus
        super().__init__("These items moved since the count started: " + ", ".join(skus))

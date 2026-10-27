class PurchasingError(Exception):
    """Base for every problem the purchasing services report. Messages are plain language."""


class PurchasingSettingsMissing(PurchasingError):
    pass

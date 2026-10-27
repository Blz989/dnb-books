class SalesError(Exception):
    """Base for every problem the sales services report. Messages are plain language."""


class SalesSettingsMissing(SalesError):
    pass


class OrderError(SalesError):
    pass


class InvoiceError(SalesError):
    pass


class ReceiptError(SalesError):
    pass


class DepositError(SalesError):
    pass

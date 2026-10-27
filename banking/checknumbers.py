"""Check numbers on a bank account: one registry shared by purchasing, sales, and printing."""


def normalize(number: str) -> str:
    """Strip spaces and leading zeros so '002050', ' 2050' and '2050' are the same check."""
    text = "".join((number or "").split()).casefold()
    return text.lstrip("0") or ("0" if text else "")

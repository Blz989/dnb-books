"""A dollar amount spelled out for the check line: 'One thousand seventy-five and 00/100'."""
from decimal import Decimal

_ONES = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen"
).split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_SCALES = ((10**9, "billion"), (10**6, "million"), (10**3, "thousand"))


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        word = _TENS[n // 10]
        parts.append(f"{word}-{_ONES[n % 10]}" if n % 10 else word)
    elif n or not parts:
        parts.append(_ONES[n])
    return " ".join(parts)


def amount_in_words(amount: Decimal) -> str:
    if amount < 0 or amount >= Decimal("1000000000000"):
        raise ValueError("The amount must be between 0.00 and 999,999,999,999.99.")
    cents_total = int((amount * 100).to_integral_value())
    dollars, cents = divmod(cents_total, 100)
    parts = []
    for size, name in _SCALES:
        count, dollars = divmod(dollars, size)
        if count:
            parts.append(f"{_below_thousand(count)} {name}")
    if dollars or not parts:
        parts.append(_below_thousand(dollars))
    words = " ".join(parts)
    return f"{words[0].upper()}{words[1:]} and {cents:02d}/100"

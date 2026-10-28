from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from core.roles import require_admin

from .models import HandlingRate, long_date


def rate_for(day: date) -> Decimal | None:
    r = HandlingRate.objects.filter(effective_from__lte=day).order_by("-effective_from").first()
    return r.rate if r else None


@transaction.atomic
def add_rate(*, effective_from: date, percent: Decimal, user) -> HandlingRate:
    require_admin(user)
    percent = Decimal(percent)
    if not (Decimal("0") <= percent <= Decimal("100")):
        raise ValidationError("Enter a rate between 0 and 100%.")
    latest = HandlingRate.objects.order_by("-effective_from").first()
    if latest and effective_from <= latest.effective_from:
        raise ValidationError(f"The new rate must start after {long_date(latest.effective_from)}.")
    fraction = (percent / 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return HandlingRate.objects.create(effective_from=effective_from, rate=fraction, created_by=user)

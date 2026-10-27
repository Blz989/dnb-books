"""The printed check: positions in inches on Letter paper, one check per sheet, on the owner's voucher stock.

The stock already carries the bank name, MICR line, lines, and check number. Only the black text is printed here.
"""
from datetime import date as date_type
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string

from core.roles import require_admin

from .models import BankAccount, CheckLayout
from .words import amount_in_words

STATIC = Path(settings.BASE_DIR) / "banking" / "static" / "banking"

# Positions from docs/design/check-mockup.html: x and y in inches from the page's top left, size in points.
DEFAULT_LAYOUT: dict[str, dict] = {
    "date": {"x": 6.15, "y": 0.86, "size": 10.5, "visible": True},
    "payee": {"x": 1.80, "y": 1.29, "size": 10.5, "visible": True},
    "amount": {"x": 6.78, "y": 1.29, "size": 10.5, "visible": True},
    "amount_words": {"x": 0.50, "y": 1.71, "size": 10.5, "visible": True},
    "address": {"x": 0.95, "y": 2.10, "size": 9.5, "visible": True},
    "memo": {"x": 0.95, "y": 2.76, "size": 9, "visible": True},
    "stub1": {"x": 0.45, "y": 3.75, "size": 9, "visible": True},
    "stub2": {"x": 0.45, "y": 7.50, "size": 9, "visible": True},
}
FIELD_LABELS = {
    "date": "Date", "payee": "Pay to", "amount": "Amount", "amount_words": "Amount in words",
    "address": "Address block", "memo": "Memo", "stub1": "Stub 1", "stub2": "Stub 2",
}
# Outline size in inches (width, height) used by the calibration print.
FIELD_BOXES = {
    "date": (1.9, 0.22), "payee": (4.5, 0.22), "amount": (1.25, 0.22), "amount_words": (6.55, 0.22),
    "address": (3.0, 0.6), "memo": (3.4, 0.22), "stub1": (7.6, 1.6), "stub2": (7.6, 1.6),
}
WORDS_WIDTH = 70
MEMO_WIDTH = 40
STUB_ROWS = 12
OFFSET_LIMIT = Decimal("0.50")
TWO_PLACES = Decimal("0.01")


class LayoutError(Exception):
    """A problem a person can fix. Messages are plain language."""


def _long_date(day: date_type) -> str:
    return f"{day:%b} {day.day}, {day.year}"


def _money(x: Decimal) -> str:
    return f"{x:,.2f}"


def layout_for(bank_account: BankAccount) -> dict:
    """Saved positions merged over the defaults, plus the offsets in inches."""
    fields = {name: dict(values) for name, values in DEFAULT_LAYOUT.items()}
    saved = CheckLayout.objects.filter(bank_account=bank_account).first()
    if saved is None:
        return {"fields": fields, "offset_x": Decimal("0.00"), "offset_y": Decimal("0.00")}
    for name, values in (saved.fields or {}).items():
        if name in fields:
            fields[name].update(values)
    return {"fields": fields, "offset_x": saved.offset_x, "offset_y": saved.offset_y}


def _is_refund(doc) -> bool:
    return hasattr(doc, "source_payment_id")


def check_context(doc, number: str) -> dict:
    """Everything printed on the check and its stubs, for a vendor payment or a customer refund."""
    from purchasing.balances import bill_total

    refund = _is_refund(doc)
    party = doc.customer if refund else doc.vendor
    address = party.billing_address if refund else party.address
    words = amount_in_words(doc.amount)
    paid_from = doc.bank_account if refund else doc.paid_from

    if refund:
        source = doc.source
        columns = ["Refund", "Source", "Amount"]
        rows = [[doc.number, getattr(source, "number", ""), _money(doc.amount)]]
        memo = doc.number
    else:
        columns = ["Bill", "Your invoice #", "Bill date", "Bill amount", "Paid"]
        applications = list(
            doc.applications.filter(removed=False).select_related("bill").order_by("bill__date", "bill__id"))
        rows = [
            [a.bill.number, a.bill.vendor_invoice_number, _long_date(a.bill.date),
             _money(bill_total(a.bill)), _money(a.amount)]
            for a in applications
        ]
        invoices = [a.bill.vendor_invoice_number for a in applications if a.bill.vendor_invoice_number]
        memo = ("Inv " + ", ".join(invoices))[:MEMO_WIDTH] if invoices else ""

    more = ""
    if len(rows) > STUB_ROWS:
        more = f"and {len(rows) - (STUB_ROWS - 1)} more (see statement)"
        rows = rows[:STUB_ROWS - 1]

    return {
        "number": number,
        "date": _long_date(doc.date),
        "payee": party.name,
        "amount": "**" + _money(doc.amount),
        "amount_words": words.ljust(WORDS_WIDTH, "*"),
        "address_lines": [party.name, *[ln.strip() for ln in (address or "").splitlines() if ln.strip()]],
        "memo": memo,
        "stub": {
            "payee": party.name,
            "summary": f"Check {number} · {_long_date(doc.date)} · ${_money(doc.amount)}",
            "columns": columns, "rows": rows, "more": more,
            "right_columns": [3, 4] if not refund else [2],
            "paid_from": f"Paid from {paid_from}",
        },
    }


def _placed(layout: dict, name: str, extra: dict | None = None) -> dict | None:
    spec = layout["fields"][name]
    if not spec.get("visible", True):
        return None
    return {
        "name": name, "left": f"{Decimal(str(spec['x'])) + layout['offset_x']:.2f}",
        "top": f"{Decimal(str(spec['y'])) + layout['offset_y']:.2f}", "size": spec["size"], **(extra or {}),
    }


def render_check_html(doc, number: str) -> str:
    bank_account = BankAccount.objects.filter(
        ledger_account=doc.bank_account if _is_refund(doc) else doc.paid_from).first()
    layout = layout_for(bank_account) if bank_account else {
        "fields": {k: dict(v) for k, v in DEFAULT_LAYOUT.items()},
        "offset_x": Decimal("0.00"), "offset_y": Decimal("0.00")}
    ctx = check_context(doc, number)
    placed = {name: _placed(layout, name) for name in DEFAULT_LAYOUT}
    return render_to_string("banking/check_pdf.html", {"c": ctx, "p": placed})


def _pdf(html: str, stylesheet: str = "check_pdf.css") -> bytes:
    from weasyprint import CSS, HTML

    css = CSS(filename=str(STATIC / stylesheet))
    return HTML(string=html, base_url=Path(settings.BASE_DIR).as_uri() + "/").write_pdf(stylesheets=[css])


def check_pdf(doc, number: str) -> bytes:
    return _pdf(render_check_html(doc, number))


def _ruler(limit: float) -> list[dict]:
    steps = int(limit / 0.25) + 1
    return [{"at": f"{i * 0.25:.2f}", "major": i % 4 == 0, "label": i // 4 if i % 4 == 0 else None}
            for i in range(steps)]


def render_calibration_html(bank_account: BankAccount) -> str:
    layout = layout_for(bank_account)
    boxes = []
    for name in DEFAULT_LAYOUT:
        spec = layout["fields"][name]
        if not spec.get("visible", True):
            continue
        width, height = FIELD_BOXES[name]
        boxes.append({
            "label": FIELD_LABELS[name],
            "left": f"{Decimal(str(spec['x'])) + layout['offset_x']:.2f}",
            "top": f"{Decimal(str(spec['y'])) + layout['offset_y']:.2f}",
            "width": f"{width:.2f}", "height": f"{height:.2f}",
        })
    return render_to_string("banking/calibration_pdf.html", {
        "account": bank_account, "boxes": boxes, "vertical": _ruler(8.5), "horizontal": _ruler(11),
        "offset_x": f"{layout['offset_x']:+.2f}", "offset_y": f"{layout['offset_y']:+.2f}",
    })


def calibration_pdf(bank_account: BankAccount) -> bytes:
    return _pdf(render_calibration_html(bank_account), "calibration_pdf.css")


def save_layout(bank_account: BankAccount, *, fields: dict, offset_x: Decimal, offset_y: Decimal, user) -> CheckLayout:
    require_admin(user)
    for offset in (offset_x, offset_y):
        if not (-OFFSET_LIMIT <= offset <= OFFSET_LIMIT):
            raise LayoutError("Offsets must be between -0.50 and 0.50 inches.")
    clean: dict[str, dict] = {}
    for name, values in fields.items():
        if name not in DEFAULT_LAYOUT:
            continue
        try:
            x, y, size = float(values["x"]), float(values["y"]), float(values["size"])
        except (KeyError, TypeError, ValueError):
            raise LayoutError(f"{FIELD_LABELS[name]}: enter a position and a font size.") from None
        if not (0 <= x <= 8.5 and 0 <= y <= 11):
            raise LayoutError(f"{FIELD_LABELS[name]}: the position must be on the page.")
        if not (6 <= size <= 20):
            raise LayoutError(f"{FIELD_LABELS[name]}: the font size must be between 6 and 20.")
        clean[name] = {"x": round(x, 2), "y": round(y, 2), "size": round(size, 1),
                       "visible": bool(values.get("visible", True))}
    with transaction.atomic():
        layout, _ = CheckLayout.objects.get_or_create(bank_account=bank_account)
        layout.fields = clean
        layout.offset_x = offset_x.quantize(TWO_PLACES)
        layout.offset_y = offset_y.quantize(TWO_PLACES)
        layout.save()
    return layout

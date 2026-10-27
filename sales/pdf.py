"""Invoice PDF: a Django template rendered to HTML, then to PDF by WeasyPrint. Fonts and the logo come from disk, so
nothing is fetched from the internet."""
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from weasyprint import HTML

from .balances import invoice_applied, invoice_total
from .models import DocStatus, Fulfillment, Invoice, SalesSettings

FONT_DIR = Path(settings.BASE_DIR) / "core" / "static" / "core" / "fonts"


def _address(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _logo_uri(config: SalesSettings) -> str | None:
    if not config.logo:
        return None
    path = Path(settings.MEDIA_ROOT) / config.logo.name
    return path.as_uri() if path.is_file() else None


def render_invoice_html(invoice: Invoice) -> str:
    config = SalesSettings.load()
    subtotal = invoice_total(invoice)
    applied = invoice_applied(invoice)
    customer = invoice.customer
    ship_lines = _address(invoice.ship_to)
    context = {
        "invoice": invoice,
        "company": config,
        "company_address": _address(config.company_address),
        "logo_uri": _logo_uri(config),
        "font_uri": FONT_DIR.as_uri(),
        "bill_name": customer.name,
        "bill_lines": _address(customer.billing_address),
        "ship_first": ship_lines[0] if ship_lines else customer.name,
        "ship_lines": ship_lines[1:],
        "lines": list(invoice.lines.select_related("item")),
        "drop_ship": Fulfillment.DROP_SHIP,
        "subtotal": subtotal,
        "applied": applied,
        "balance": subtotal - applied,
        "is_void": invoice.status == DocStatus.VOID,
    }
    return render_to_string("sales/invoice_pdf.html", context)


def invoice_document(invoice: Invoice):
    return HTML(string=render_invoice_html(invoice), base_url=Path(settings.BASE_DIR).as_uri() + "/").render()


def invoice_pdf(invoice: Invoice) -> bytes:
    return invoice_document(invoice).write_pdf()

"""Invoice PDF: a Django template rendered to HTML, then to PDF by WeasyPrint. Fonts and the logo come from disk, so
nothing is fetched from the internet."""
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from weasyprint import CSS, HTML

from .balances import invoice_applied, invoice_total
from .models import DocStatus, Fulfillment, Invoice, SalesSettings

FONT_DIR = Path(settings.BASE_DIR) / "core" / "static" / "core" / "fonts"
INVOICE_CSS = Path(settings.BASE_DIR) / "sales" / "static" / "sales" / "invoice_pdf.css"


def _address(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _logo_uri(config: SalesSettings) -> str | None:
    if not config.logo:
        return None
    path = Path(settings.MEDIA_ROOT) / config.logo.name
    return path.as_uri() if path.is_file() else None


def _grouped_lines(invoice: Invoice) -> list[dict]:
    """Lines grouped by item category: uncategorized first with no heading, then each category once, A to Z.
    Within a group lines keep their entered order; numbering runs on across groups."""
    lines = list(invoice.lines.select_related("item", "item__category"))
    groups: dict[str, list] = {}
    for line in lines:
        category = line.item.category
        groups.setdefault(category.name if category else "", []).append(line)
    number = 0
    result = []
    for name in sorted(groups, key=lambda n: (n != "", n.lower())):
        rows = []
        for line in groups[name]:
            number += 1
            rows.append({"n": number, "line": line})
        result.append({"heading": name, "rows": rows})
    return result


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
        "groups": _grouped_lines(invoice),
        "items_total": sum(l.quantity for l in invoice.lines.all()),
        "drop_ship": Fulfillment.DROP_SHIP,
        "subtotal": subtotal,
        "applied": applied,
        "balance": subtotal - applied,
        "is_void": invoice.status == DocStatus.VOID,
    }
    return render_to_string("sales/invoice_pdf.html", context)


def invoice_document(invoice: Invoice):
    html = HTML(string=render_invoice_html(invoice), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.render(stylesheets=[CSS(filename=str(INVOICE_CSS))])


def invoice_pdf(invoice: Invoice) -> bytes:
    return invoice_document(invoice).write_pdf()


STATEMENT_CSS = Path(settings.BASE_DIR) / "sales" / "static" / "sales" / "statement_pdf.css"


def render_statement_html(statement) -> str:
    config = SalesSettings.load()
    customer = statement.customer
    context = {
        "statement": statement,
        "company": config,
        "company_address": _address(config.company_address),
        "logo_uri": _logo_uri(config),
        "font_uri": FONT_DIR.as_uri(),
        "bill_lines": _address(customer.billing_address),
    }
    return render_to_string("sales/statement_pdf.html", context)


def statement_pdf(statement) -> bytes:
    html = HTML(string=render_statement_html(statement), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.render(stylesheets=[CSS(filename=str(STATEMENT_CSS))]).write_pdf()

"""Invoice PDF: a Django template rendered to HTML, then to PDF by WeasyPrint. Fonts and the logo come from disk, so
nothing is fetched from the internet. The layout (template and stylesheet) follows the Sales setting, read on every
render, so a reprint of any invoice uses the layout chosen now."""
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from weasyprint import CSS, HTML

from core.company import logo_uri

from .balances import invoice_applied, invoice_total
from .models import DocStatus, Fulfillment, Invoice, InvoiceLayout, SalesSettings

FONT_DIR = Path(settings.BASE_DIR) / "core" / "static" / "core" / "fonts"
INVOICE_CSS = Path(settings.BASE_DIR) / "sales" / "static" / "sales" / "invoice_pdf.css"
INVOICE_MODERN_CSS = INVOICE_CSS.with_name("invoice_pdf_modern.css")
INVOICE_LAYOUTS = {
    InvoiceLayout.CLASSIC: ("sales/invoice_pdf.html", INVOICE_CSS),
    InvoiceLayout.MODERN: ("sales/invoice_pdf_modern.html", INVOICE_MODERN_CSS),
}


def _layout() -> tuple[str, Path]:
    return INVOICE_LAYOUTS.get(SalesSettings.load().invoice_layout, INVOICE_LAYOUTS[InvoiceLayout.CLASSIC])


def _address(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


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
        "logo_uri": logo_uri(),
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
        "expected_delivery": invoice.sales_order.expected_delivery_date if invoice.sales_order else None,
    }
    return render_to_string(_layout()[0], context)


def invoice_document(invoice: Invoice):
    html = HTML(string=render_invoice_html(invoice), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.render(stylesheets=[CSS(filename=str(_layout()[1]))])


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
        "logo_uri": logo_uri(),
        "font_uri": FONT_DIR.as_uri(),
        "bill_lines": _address(customer.billing_address),
    }
    return render_to_string("sales/statement_pdf.html", context)


def statement_pdf(statement) -> bytes:
    html = HTML(string=render_statement_html(statement), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.render(stylesheets=[CSS(filename=str(STATEMENT_CSS))]).write_pdf()

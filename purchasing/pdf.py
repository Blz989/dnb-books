"""Purchase order PDF, laid out like the invoice PDF and rendered by WeasyPrint."""
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from core.company import logo_uri
from sales.models import SalesSettings
from sales.pdf import FONT_DIR, INVOICE_CSS, _address

from .models import PurchaseOrder


def render_po_html(order: PurchaseOrder) -> str:
    company = SalesSettings.load()
    lines = list(order.lines.select_related("item").order_by("position", "id"))
    rows = [{"n": n, "line": line, "amount": line.quantity * line.price} for n, line in enumerate(lines, 1)]
    vendor = order.vendor
    if order.sales_order_id:
        # Drop ship: the vendor ships straight to the customer.
        ship_name = order.sales_order.customer.name
        ship_lines = _address(order.sales_order.ship_to) or _address(order.sales_order.customer.ship_to_address)
        if ship_lines and ship_lines[0] == ship_name:
            ship_lines = ship_lines[1:]
    else:
        ship_name, ship_lines = company.company_name or "Your company", _address(company.company_address)
    return render_to_string("purchasing/po_pdf.html", {
        "order": order,
        "company": company,
        "company_address": _address(company.company_address),
        "logo_uri": logo_uri(),
        "font_uri": FONT_DIR.as_uri(),
        "vendor": vendor,
        "vendor_lines": _address(vendor.address),
        "ship_name": ship_name,
        "ship_lines": ship_lines,
        "drop_ship": bool(order.sales_order_id),
        "rows": rows,
        "items_total": sum(line.quantity for line in lines),
        "total": sum((r["amount"] for r in rows), 0),
    })


def po_pdf(order: PurchaseOrder) -> bytes:
    from weasyprint import CSS, HTML

    html = HTML(string=render_po_html(order), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.write_pdf(stylesheets=[CSS(filename=str(INVOICE_CSS))])

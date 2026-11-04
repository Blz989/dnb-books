"""Purchase order PDF, laid out like the invoice PDF and rendered by WeasyPrint."""
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from core.company import logo_uri
from sales.models import SalesSettings
from sales.pdf import FONT_DIR, INVOICE_CSS, _address

from .models import PurchaseOrder, PurchaseOrderAllocation


def _ship_to(so) -> tuple[str, list[str]]:
    name = so.customer.name
    lines = _address(so.ship_to) or _address(so.customer.ship_to_address)
    if lines and lines[0] == name:
        lines = lines[1:]
    return name, lines


def render_po_html(order: PurchaseOrder) -> str:
    company = SalesSettings.load()
    lines = list(order.lines.select_related("item").order_by("position", "id"))
    rows = [{"n": n, "line": line, "amount": line.quantity * line.price} for n, line in enumerate(lines, 1)]
    vendor = order.vendor
    sales_orders = order.sales_orders()
    shipments = []
    if len(sales_orders) == 1:
        # Drop ship: the vendor ships straight to the customer.
        ship_name, ship_lines = _ship_to(sales_orders[0])
    elif sales_orders:
        ship_name, ship_lines = "Several customers", ["See the ship-to list below."]
        allocations = PurchaseOrderAllocation.objects.filter(po_line__order=order).select_related(
            "po_line__item", "so_line__order")
        for so in sales_orders:
            name, address = _ship_to(so)
            items = [(a.po_line.item.sku, a.po_line.description or a.po_line.item.name, a.quantity)
                     for a in allocations if a.so_line.order_id == so.pk]
            shipments.append({"order": so, "name": name, "lines": address, "items": items})
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
        "drop_ship": bool(sales_orders),
        "sales_order_numbers": ", ".join(so.number for so in sales_orders),
        "shipments": shipments,
        "rows": rows,
        "items_total": sum(line.quantity for line in lines),
        "total": sum((r["amount"] for r in rows), 0),
    })


def po_pdf(order: PurchaseOrder) -> bytes:
    from weasyprint import CSS, HTML

    html = HTML(string=render_po_html(order), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.write_pdf(stylesheets=[CSS(filename=str(INVOICE_CSS))])

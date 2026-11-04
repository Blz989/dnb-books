"""Download any report as a PDF: the report page is rendered in print layout and handed to WeasyPrint."""
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse

PDF_CSS = Path(settings.BASE_DIR) / "core" / "static" / "core" / "report_pdf.css"


def pdf_export(view):
    """Wrap a report view. With ?format=pdf the view renders in print layout and the HTML becomes a PDF."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.GET.get("format") != "pdf":
            return view(request, *args, **kwargs)
        request.report_pdf = True
        response = view(request, *args, **kwargs)
        if response.status_code != 200 or not response.get("Content-Type", "").startswith("text/html"):
            return response
        from weasyprint import CSS, HTML

        html = HTML(string=response.content.decode(), base_url=Path(settings.BASE_DIR).as_uri() + "/")
        pdf = html.write_pdf(stylesheets=[CSS(filename=str(PDF_CSS))])
        out = HttpResponse(pdf, content_type="application/pdf")
        name = request.resolver_match.url_name if request.resolver_match else "report"
        out["Content-Disposition"] = f'inline; filename="{name}.pdf"'
        return out

    return wrapper

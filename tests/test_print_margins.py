import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def page_margin(path: str) -> str:
    css = (ROOT / path).read_text()
    return re.search(r"@page\s*\{[^}]*?margin:\s*([^;]+);", css).group(1).strip()


def test_invoice_and_statement_print_a_quarter_inch_from_the_edge():
    for path in ("sales/static/sales/invoice_pdf.css", "sales/static/sales/statement_pdf.css",
                 "docs/design/invoice-mockup.html"):
        assert page_margin(path) == "0.25in 0.25in 0.4in", path


def test_report_pdfs_use_tight_margins():
    assert page_margin("core/static/core/report_pdf.css") == "8mm 8mm 12mm"


def test_checks_keep_their_exact_layout():
    assert page_margin("banking/static/banking/check_pdf.css") == "0"

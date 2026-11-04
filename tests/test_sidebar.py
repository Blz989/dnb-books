import re
from pathlib import Path

from django.urls import reverse

from sales.models import SalesSettings

CSS = Path(__file__).resolve().parent.parent / "core" / "static" / "core" / "app.css"
LONG_NAME = "D&B Goods Trading Corporation"


def test_home_shows_full_company_name(client, standard_user):
    config = SalesSettings.load()
    config.company_name = LONG_NAME
    config.save()
    client.force_login(standard_user)
    html = client.get(reverse("home")).content.decode()
    assert "D&amp;B Goods Trading Corporation" in html


def test_sidebar_is_wide_and_brand_name_wraps():
    css = CSS.read_text()
    assert "grid-template-columns: 264px 1fr" in css
    rule = re.search(r"\.brand-name\s*\{([^}]*)\}", css).group(1)
    assert "white-space: nowrap" not in rule
    assert "-webkit-line-clamp: 2" in rule

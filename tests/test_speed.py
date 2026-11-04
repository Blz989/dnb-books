"""Speed over the Cloudflare tunnel: long-lived static files, hover prefetch, and query counts that don't grow per row."""
import json
import re
from collections import deque
from html.parser import HTMLParser

import pytest
from django.core.management import call_command
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.templatetags.static import static
from django.urls import reverse

from core.middleware import StaticCacheMiddleware

YEAR = "public, max-age=31536000, immutable"


# ---- 1. static files ----

def _static_response(path):
    middleware = StaticCacheMiddleware(lambda r: HttpResponse("x", headers={"Cache-Control": "max-age=60, public"}))
    return middleware(RequestFactory().get(path))


def test_versioned_static_file_is_kept_for_a_year():
    assert _static_response(static("core/app.css"))["Cache-Control"] == YEAR


def test_static_file_with_an_old_version_keeps_the_short_cache():
    assert _static_response("/static/core/app.css?v=0000000000")["Cache-Control"] == "max-age=60, public"


def test_static_file_without_a_version_keeps_the_short_cache():
    assert _static_response("/static/core/app.css")["Cache-Control"] == "max-age=60, public"


def test_fonts_are_kept_for_a_year():
    assert _static_response("/static/core/fonts/inter-latin-wght-normal.woff2")["Cache-Control"] == YEAR


def test_pages_are_not_given_the_long_cache():
    middleware = StaticCacheMiddleware(lambda r: HttpResponse("x"))
    assert "Cache-Control" not in middleware(RequestFactory().get("/items/?v=abc"))


# ---- 2. hover prefetch ----

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.rules = [], []
        self._in_rules = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs)
        self._in_rules = tag == "script" and attrs.get("type") == "speculationrules"

    def handle_data(self, data):
        if self._in_rules:
            self.rules.append(data)

    def handle_endtag(self, tag):
        self._in_rules = False


def _parse(html):
    p = _Links()
    p.feed(html)
    return p


def test_pages_prefetch_links_on_hover(client, standard_user):
    client.force_login(standard_user)
    rules = json.loads("".join(_parse(client.get(reverse("home")).content.decode()).rules))
    rule = rules["prefetch"][0]
    assert rule["eagerness"] == "moderate"
    excluded = rule["where"]["and"][1]["not"]["selector_matches"]
    for part in ("pdf", "format=", "/accounts/", "[download]", "[data-no-prefetch]"):
        assert part in excluded


def test_sign_in_page_does_not_prefetch(client, db):
    assert not _parse(client.get(reverse("login")).content.decode()).rules


def _prefetched(attrs) -> bool:
    """Python mirror of the speculation rule: same-site links not excluded by the selector."""
    href = attrs["href"]
    if not href.startswith("/") or href.startswith("//"):
        return False
    if "download" in attrs or "data-no-prefetch" in attrs or attrs.get("target") == "_blank":
        return False
    return not any(s in href for s in ("pdf", "format=", "/accounts/"))


@pytest.mark.django_db
def test_no_prefetched_link_writes_to_the_database(client):
    """Hover prefetch opens links nobody clicked, so every link it can reach must be read-only."""
    call_command("seed_demo", verbosity=0)
    client.login(username="demo", password="demo-pass-123")
    writes, start = [], ["/", "/settings/advanced/"]

    def watch(execute, sql, params, many, context):
        # A settings page creates its one row of defaults the first time it opens; a hover doing that is harmless.
        first_defaults = re.match(r'\s*INSERT INTO "\w+settings"', sql)
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)", sql, re.I) and "django_session" not in sql and not first_defaults:
            writes.append(sql[:120])
        return execute(sql, params, many, context)

    seen, queue, visited = set(start), deque(start), 0
    with connection.execute_wrapper(watch):
        while queue and visited < 600:
            path = queue.popleft()
            response = client.get(path)
            visited += 1
            assert not writes, f"Opening {path} wrote to the database: {writes}"
            if response.status_code != 200 or "text/html" not in response.get("Content-Type", ""):
                continue
            for attrs in _parse(response.content.decode()).links:
                href = attrs["href"].split("#")[0]
                if href and _prefetched(attrs) and href not in seen:
                    seen.add(href)
                    queue.append(href)
    assert visited > 50


# ---- 3. query counts ----

def _queries(client, path) -> int:
    with CaptureQueriesContext(connection) as q:
        assert client.get(path).status_code == 200
    return len(q)


@pytest.mark.django_db
def test_inbox_queries_do_not_grow_with_rows(client):
    from banking.models import BankTransaction, TxnStatus

    call_command("seed_demo", verbosity=0)
    client.login(username="demo", password="demo-pass-123")
    new = BankTransaction.objects.filter(status=TxnStatus.NEW)
    accounts = new.values("bank_account_id").distinct()
    smallest = min(accounts, key=lambda a: new.filter(bank_account_id=a["bank_account_id"]).count())
    one = new.filter(bank_account_id=smallest["bank_account_id"]).count()
    assert new.count() - one >= 5
    few = _queries(client, f"{reverse('banking:inbox')}?account={smallest['bank_account_id']}")
    all_rows = _queries(client, reverse("banking:inbox"))
    assert all_rows - few <= 3


@pytest.mark.django_db
def test_item_list_queries_do_not_grow_with_rows(client):
    from items.models import Item

    call_command("seed_demo", verbosity=0)
    client.login(username="demo", password="demo-pass-123")
    first = Item.objects.filter(is_active=True).order_by("sku").first()
    assert Item.objects.filter(is_active=True).count() >= 20
    one = _queries(client, f"{reverse('items:list')}?q={first.sku}")
    many = _queries(client, reverse("items:list"))
    assert many - one <= 3


@pytest.mark.django_db
def test_batch_unit_costs_match_one_at_a_time():
    from items.models import Item
    from items.stock import current_unit_cost, current_unit_costs

    call_command("seed_demo", verbosity=0)
    items = list(Item.objects.all())
    stock = [i for i in items if i.is_stock]
    assert stock
    assert current_unit_costs(items) == {i.pk: current_unit_cost(i) for i in stock}

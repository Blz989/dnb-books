"""Signing in through a public address (Cloudflare Tunnel): Django must trust the https origin the browser sends."""
import re

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

PUBLIC = "books.lopezhome.casa"
ORIGIN = f"https://{PUBLIC}"


def _login(host):
    c = Client(enforce_csrf_checks=True)
    page = c.get("/accounts/login/", HTTP_HOST=host).content.decode()
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
    return c.post("/accounts/login/", {"username": "owner", "password": "pw-long-enough-1",
                                       "csrfmiddlewaretoken": token},
                  HTTP_HOST=host, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=f"{ORIGIN}/accounts/login/")


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_user("owner", password="pw-long-enough-1")


def test_login_through_tunnel_refused_without_trusted_origin(owner, settings):
    settings.ALLOWED_HOSTS = [PUBLIC]
    settings.CSRF_TRUSTED_ORIGINS = []
    assert _login(PUBLIC).status_code == 403


def test_login_through_tunnel_works_with_trusted_origin(owner, settings):
    settings.ALLOWED_HOSTS = [PUBLIC]
    settings.CSRF_TRUSTED_ORIGINS = [ORIGIN]
    r = _login(PUBLIC)
    assert r.status_code == 302 and r["Location"] == "/"


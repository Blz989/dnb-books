import asyncio
import io

import pytest
from django.urls import reverse
from PIL import Image

from core import favicon
from core.company import replace_logo
from sales.models import SalesSettings


def logo_png(color=(200, 30, 60, 255), size=(300, 120)) -> bytes:
    """A wide logo on a white border, like a scanned JPEG: the border should be trimmed, the shape kept."""
    img = Image.new("RGBA", (size[0] + 40, size[1] + 40), (255, 255, 255, 255))
    img.paste(Image.new("RGBA", size, color), (20, 20))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def with_logo(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    config = SalesSettings.load()
    replace_logo(config, logo_png(), "png")
    config.save()
    favicon.clear_cache()
    yield config
    favicon.clear_cache()


@pytest.fixture
def no_logo(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    favicon.clear_cache()
    yield
    favicon.clear_cache()


def image(response) -> Image.Image:
    return Image.open(io.BytesIO(b"".join(response.streaming_content) if response.streaming else response.content))


def test_favicon_ico_needs_no_login(client, no_logo):
    response = client.get("/favicon.ico")
    assert response.status_code == 200 and response["Content-Type"] == "image/x-icon"
    img = image(response)
    assert img.format == "ICO" and (32, 32) in img.info["sizes"] and (16, 16) in img.info["sizes"]
    assert "max-age=86400" in response["Cache-Control"] and response["ETag"]


@pytest.mark.parametrize("size", [32, 180, 192])
def test_png_icons(client, no_logo, size):
    response = client.get(reverse("brand-icon", args=[size]))
    assert response.status_code == 200 and response["Content-Type"] == "image/png"
    assert image(response).size == (size, size)


def test_other_sizes_are_not_served(client, no_logo):
    assert client.get("/brand/icon-64.png").status_code == 404


def test_uses_the_uploaded_logo_scaled_to_fit_and_centered(with_logo):
    img = Image.open(io.BytesIO(favicon.render_png(32))).convert("RGBA")
    assert img.size == (32, 32)
    # The logo's red fills the width; the top and bottom rows are transparent padding (2.5:1 logo, not stretched).
    assert img.getpixel((16, 16))[:3] == (200, 30, 60)
    assert img.getpixel((16, 1))[3] == 0 and img.getpixel((16, 30))[3] == 0
    # The white border of the upload was trimmed, so no white shows around the mark.
    assert img.getpixel((3, 16))[:3] != (255, 255, 255)


def test_fallback_mark_without_a_logo(no_logo):
    img = Image.open(io.BytesIO(favicon.render_png(32))).convert("RGBA")
    assert img.getpixel((3, 16))[:3] == favicon.ACCENT  # the green tile
    assert favicon.icon_version() == favicon.DEFAULT_VERSION


def test_apple_touch_icon_is_opaque(with_logo):
    img = Image.open(io.BytesIO(favicon.render_png(180))).convert("RGBA")
    assert img.getpixel((2, 2)) == (255, 255, 255, 255)


def test_version_changes_when_the_logo_is_replaced(with_logo):
    first = favicon.icon_version()
    replace_logo(with_logo, logo_png(color=(10, 90, 200, 255)), "png")
    with_logo.save()
    favicon.clear_cache()
    assert favicon.icon_version() != first


def test_etag_answers_not_modified(client, no_logo):
    tag = client.get("/favicon.ico")["ETag"]
    assert client.get("/favicon.ico", HTTP_IF_NONE_MATCH=tag).status_code == 304


def test_versioned_png_is_cached_for_a_year(client, no_logo):
    v = favicon.icon_version()
    response = client.get(reverse("brand-icon", args=[32]), {"v": v})
    assert "max-age=31536000" in response["Cache-Control"] and "immutable" in response["Cache-Control"]


def test_base_and_login_pages_link_the_icons(client, standard_user, no_logo):
    v = favicon.icon_version()
    login = client.get(reverse("login")).content.decode()
    for html in (login, (client.force_login(standard_user), client.get(reverse("home")))[1].content.decode()):
        assert f'<link rel="icon" type="image/png" sizes="32x32" href="/brand/icon-32.png?v={v}">' in html
        assert '<link rel="icon" href="/favicon.ico" sizes="any">' in html
        assert f'<link rel="apple-touch-icon" href="/brand/icon-180.png?v={v}">' in html


def test_connector_serves_the_favicon_without_a_token(no_logo):
    from mcpserver.server import AccessMiddleware

    async def inner(scope, receive, send):
        raise AssertionError("the MCP app must not see a favicon request")

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/favicon.ico", "headers": []}
    asyncio.run(AccessMiddleware(inner)(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert start["status"] == 200 and (b"content-type", b"image/x-icon") in start["headers"]
    assert Image.open(io.BytesIO(body)).format == "ICO"

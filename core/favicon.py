"""Browser and home-screen icons drawn from the company logo (Settings, Company profile).

The logo is trimmed of its plain border, scaled to fit a square without stretching, and centered on transparent
padding. With no logo the icon is a "D&B" mark in the app's green. Icons are rebuilt only when the logo changes.
"""
import hashlib
import io
import os
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageChops, ImageDraw, ImageFont

ACCENT = (47, 107, 79)  # --accent, #2f6b4f
PNG_SIZES = (32, 180, 192)
ICO_SIZES = (16, 32, 48)
APPLE_SIZE = 180
REV = "1"  # bump when the drawing below changes, so browsers fetch the new icons
DEFAULT_VERSION = f"d{REV}"
FONT = Path(settings.BASE_DIR) / "core" / "static" / "core" / "fonts" / "inter-latin-wght-normal.woff2"
TRIM_TOLERANCE = 24

_cache: dict[tuple[str, object], bytes] = {}


def clear_cache() -> None:
    _cache.clear()


def _logo_path(config=None) -> str | None:
    if config is None:
        from sales.models import SalesSettings

        config = SalesSettings.objects.filter(pk=1).only("logo").first()
    if not config or not config.logo:
        return None
    path = os.path.join(settings.MEDIA_ROOT, config.logo.name)
    return path if os.path.exists(path) else None


def icon_version(config=None) -> str:
    """Short tag that changes whenever the logo file is replaced. Used as the ETag and the ?v= on icon links."""
    path = _logo_path(config)
    if path is None:
        return DEFAULT_VERSION
    stat = os.stat(path)
    return hashlib.sha1(f"{REV}|{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:10]


def _trimmed_logo(path: str) -> Image.Image | None:
    try:
        img = Image.open(path)
        img.load()
    except Exception:  # noqa: BLE001 - an unreadable upload falls back to the D&B mark
        return None
    img = img.convert("RGBA")
    corner = img.getpixel((0, 0))
    if corner[3] == 0:
        box = img.getchannel("A").getbbox()
    else:
        # Trim a plain border the same color as the corner (a white JPEG background), keep everything else.
        bg = Image.new("RGB", img.size, corner[:3])
        diff = ImageChops.difference(img.convert("RGB"), bg).convert("L").point(lambda v: 255 if v > TRIM_TOLERANCE else 0)
        box = diff.getbbox()
    return img.crop(box) if box else img


def _fit(logo: Image.Image, size: int, background) -> Image.Image:
    # Small icons get a thin margin so the mark stays as large as possible; the home-screen icon gets more air.
    pad = round(size * 0.1) if size >= 120 else max(1, size // 16)
    room = size - 2 * pad
    scale = min(room / logo.width, room / logo.height)
    w, h = max(1, round(logo.width * scale)), max(1, round(logo.height * scale))
    canvas = Image.new("RGBA", (size, size), background)
    canvas.alpha_composite(logo.resize((w, h), Image.LANCZOS), ((size - w) // 2, (size - h) // 2))
    return canvas


def _mark(size: int) -> Image.Image:
    """The fallback: white "D&B" in Inter bold on the accent green."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    radius = 0 if size >= 120 else round(size * 0.18)  # iOS rounds the home-screen icon itself
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=ACCENT + (255,))
    text = "D&B"
    font_size = size
    while font_size > 4:
        font = ImageFont.truetype(str(FONT), font_size)
        try:
            font.set_variation_by_axes([800])
        except Exception:  # noqa: BLE001 - a static font copy has no weight axis
            pass
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= size * 0.82:
            break
        font_size -= 1
    x = (size - (right - left)) / 2 - left
    y = (size - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return canvas


def _icon(size: int, config=None) -> Image.Image:
    path = _logo_path(config)
    logo = _trimmed_logo(path) if path else None
    if logo is None:
        return _mark(size)
    # iOS shows a transparent home-screen icon on black, so that one sits on white like the sidebar logo.
    background = (255, 255, 255, 255) if size >= APPLE_SIZE else (0, 0, 0, 0)
    return _fit(logo, size, background)


def _cached(kind, build) -> bytes:
    key = (icon_version(), kind)
    if key not in _cache:
        _cache[key] = build()
    return _cache[key]


def render_png(size: int) -> bytes:
    def build():
        out = io.BytesIO()
        _icon(size).save(out, "PNG", optimize=True)
        return out.getvalue()

    return _cached(size, build)


def render_ico() -> bytes:
    def build():
        images = [_icon(s) for s in ICO_SIZES]
        out = io.BytesIO()
        images[-1].save(out, "ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=images[:-1])
        return out.getvalue()

    return _cached("ico", build)

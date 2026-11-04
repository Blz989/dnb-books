"""Company profile (name, contact details, logo). Stored on SalesSettings, shown app-wide and on every PDF."""
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

from sales.models import SalesSettings


def profile() -> SalesSettings:
    return SalesSettings.load()


def logo_uri() -> str | None:
    """file:// URI of the logo for WeasyPrint, or None when there is no logo on disk."""
    config = profile()
    if not config.logo:
        return None
    path = Path(settings.MEDIA_ROOT) / config.logo.name
    return path.as_uri() if path.is_file() else None


def replace_logo(config: SalesSettings, data: bytes, extension: str) -> None:
    if config.logo:
        config.logo.delete(save=False)
    config.logo.save(f"company-logo.{extension}", ContentFile(data), save=False)


def remove_logo(config: SalesSettings) -> None:
    if config.logo:
        config.logo.delete(save=False)
    config.logo = ""

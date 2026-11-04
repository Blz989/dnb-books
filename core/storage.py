"""Static file URLs carry a hash of the file, so a new release always fetches its own CSS and JavaScript.

Without it Cloudflare's browser cache (4 hours by default) can pair new pages with old CSS.
"""
import hashlib
import os

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import StaticFilesStorage

_cache: dict[tuple[str, float], str] = {}


def file_version(path: str) -> str:
    """First 10 hex digits of the file's sha256, remembered until the file changes."""
    key = (path, os.path.getmtime(path))
    if key not in _cache:
        with open(path, "rb") as f:
            _cache[key] = hashlib.sha256(f.read()).hexdigest()[:10]
    return _cache[key]


class VersionedStaticFilesStorage(StaticFilesStorage):
    def url(self, name):
        url = super().url(name)
        path = self.path(name) if self.exists(name) else finders.find(name)
        return f"{url}?v={file_version(path)}" if path else url


def current_version(name: str) -> str | None:
    """The ?v= value a page links today for static file `name`, or None if there is no such file."""
    from django.contrib.staticfiles.storage import staticfiles_storage

    path = staticfiles_storage.path(name) if staticfiles_storage.exists(name) else finders.find(name)
    return file_version(path) if path else None

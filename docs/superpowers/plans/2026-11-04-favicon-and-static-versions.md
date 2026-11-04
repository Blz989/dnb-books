# Favicon and versioned static files: plan

Owner request (2026-11-04, after PR 39): a favicon from the uploaded company logo, and a fix for the phone Menu button showing unstyled on desktop.
PR title: "Favicon from the company logo, and versioned static files"

## Global Constraints
- No new dependencies. Pillow is already installed (WeasyPrint depends on it).
- Icons are public (no login), cached, and change when the logo is replaced.
- The logo design is not altered: scaled to fit, centered, transparent padding; uniform border trimmed.
- Screenshots of the rendered icons; never the real `data/` database.

## Task 1: Versioned static URLs
Cause of the desktop bug: `/static/core/app.css` keeps one URL across releases, and Cloudflare's browser cache TTL (4 hours by default) keeps the old file, so new HTML (the Menu button) meets old CSS (no rule hiding it).
- [x] `core/storage.py`: `VersionedStaticFilesStorage` adds `?v=<first 10 hex of sha256 of the file>` to every `{% static %}` URL; `STORAGES["staticfiles"]` uses it.
- [x] Tests: `tests/test_static_versions.py` (URL carries the hash, the hash changes with the content, base page links versioned CSS).
- [x] Commit: `Version static file URLs so an update never meets old CSS`

## Task 2: Favicon
- [x] `core/favicon.py`: `icon_version()`, `render_png(size)`, `render_ico()`; logo from `SalesSettings.logo`; fallback "D&B" mark in the accent green with Inter bold.
- [x] Routes: `/favicon.ico` (16, 32, 48), `/brand/icon-<size>.png` for 32, 180, 192. Public. ETag = version; `?v=` URLs cached a year, `/favicon.ico` a day.
- [x] `base.html` (and so the sign-in page) links `rel="icon"` 32px PNG, `/favicon.ico`, and `rel="apple-touch-icon"` 180px (opaque white, since iOS fills transparency with black).
- [x] Claude connector (`mcpserver`): `AccessMiddleware` answers `GET /favicon.ico` with the same icon before the token check.
- [x] Tests: `tests/test_favicon.py`.
- [x] Screenshots: icons at 16, 32, 180 for the uploaded-logo and fallback cases, and a tab strip.
- [x] Commit: `Favicon from the company logo`

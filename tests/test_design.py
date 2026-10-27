import re
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


def test_base_has_theme_toggle_and_tokens(client, standard_user):
    client.force_login(standard_user)
    html = client.get("/").content.decode()
    assert 'data-theme-choice="auto"' in html and 'data-theme-choice="dark"' in html
    assert "core/app.css" in html


def test_css_defines_tokens_for_both_themes():
    css = Path("core/static/core/app.css").read_text()
    for token in ["--bg", "--surface", "--text", "--muted", "--accent", "--warn-text-strong"]:
        assert css.count(token + ":") >= 2  # light and dark
    assert "prefers-color-scheme: dark" in css and '[data-theme="dark"]' in css
    assert "#6f655a" in css  # spec 6.1 muted (light)


def test_no_hard_coded_colors_in_templates():
    for p in Path(".").glob("*/templates/**/*.html"):
        assert not re.search(r"#[0-9a-fA-F]{3,6}\b", p.read_text()), p


def test_reports_still_work_after_restyle(client, admin_user, sample_ledger):
    client.force_login(admin_user)
    r = client.get("/reports/trial-balance/?as_of=2026-01-31")
    assert r.status_code == 200 and b"11,500.00" in r.content


def test_seed_demo_refuses_non_empty_ledger(posted_entry):
    with pytest.raises(CommandError, match="only runs on an empty database"):
        call_command("seed_demo")

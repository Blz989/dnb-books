def test_anonymous_home_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


def test_logged_in_home_ok(client, standard_user):
    client.force_login(standard_user)
    assert client.get("/").status_code == 200


def test_sqlite_options_for_concurrency(settings):
    opts = settings.DATABASES["default"]["OPTIONS"]
    assert "journal_mode=WAL" in opts["init_command"]
    assert opts["transaction_mode"] == "IMMEDIATE"
    assert opts["timeout"] >= 20


def test_missing_secret_key_stops_startup():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DNB_SECRET_KEY": "", "DNB_DEBUG": ""}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    r = subprocess.run([sys.executable, "manage.py", "check"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "DNB_SECRET_KEY" in r.stderr


def test_placeholder_secret_key_is_refused():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "DNB_SECRET_KEY": "change-me", "DNB_DEBUG": ""}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    r = subprocess.run([sys.executable, "manage.py", "check"], cwd=root, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "DNB_SECRET_KEY" in r.stderr

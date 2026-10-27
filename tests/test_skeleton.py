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

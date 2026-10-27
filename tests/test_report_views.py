import pytest


@pytest.fixture
def logged_in(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.mark.parametrize("path", [
    "/reports/trial-balance/", "/reports/profit-and-loss/", "/reports/balance-sheet/",
    "/reports/general-ledger/",
])
def test_reports_require_login(client, path):
    r = client.get(path)
    assert r.status_code == 302 and "/accounts/login/" in r["Location"]


def test_report_csv(client, admin_user, sample_ledger):
    client.force_login(admin_user)
    r = client.get("/reports/trial-balance/?as_of=2026-01-31&format=csv")
    assert r["Content-Type"].startswith("text/csv") and b"11,500.00" not in r.content and b"11500.00" in r.content
    assert 'filename="trial-balance-2026-01-31.csv"' in r["Content-Disposition"]


def test_trial_balance_html_has_thousands_separators(logged_in, sample_ledger):
    r = logged_in.get("/reports/trial-balance/?as_of=2026-01-31")
    assert r.status_code == 200 and b"11,500.00" in r.content


def test_standard_user_can_view_reports(client, standard_user, sample_ledger):
    client.force_login(standard_user)
    assert client.get("/reports/trial-balance/?as_of=2026-01-31").status_code == 200


def test_bad_date_shows_message(client, admin_user):
    client.force_login(admin_user)
    r = client.get("/reports/balance-sheet/?as_of=31/01/2026")
    assert r.status_code == 200 and b"Enter a date like 2026-01-31." in r.content


def test_bad_date_with_csv_still_no_500(logged_in):
    r = logged_in.get("/reports/profit-and-loss/?start=nope&end=2026-01-31&format=csv")
    assert r.status_code == 200 and b"Enter a date like 2026-01-31." in r.content


def test_defaults_when_no_dates_given(logged_in, sample_ledger):
    for path in ("trial-balance", "profit-and-loss", "balance-sheet"):
        assert logged_in.get(f"/reports/{path}/").status_code == 200


def test_profit_and_loss_page_and_csv(logged_in, sample_ledger):
    r = logged_in.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31")
    assert b"500.00" in r.content and b"200.00" in r.content
    csv = logged_in.get("/reports/profit-and-loss/?start=2026-01-01&end=2026-01-31&format=csv")
    assert b"200.00" in csv.content
    assert 'filename="profit-and-loss-2026-01-01_2026-01-31.csv"' in csv["Content-Disposition"]


def test_balance_sheet_page_and_csv(logged_in, sample_ledger):
    r = logged_in.get("/reports/balance-sheet/?as_of=2026-01-31")
    assert b"11,200.00" in r.content
    csv = logged_in.get("/reports/balance-sheet/?as_of=2026-01-31&format=csv")
    assert b"11200.00" in csv.content and b"11,200.00" not in csv.content


def test_general_ledger_page_and_csv(logged_in, sample_ledger, cash):
    url = f"/reports/general-ledger/?account={cash.pk}&start=2026-01-01&end=2026-01-31"
    r = logged_in.get(url)
    assert b"11,000.00" in r.content and b"10,700.00" in r.content
    csv = logged_in.get(url + "&format=csv")
    assert b"10700.00" in csv.content
    assert "general-ledger-1000-2026-01-01_2026-01-31.csv" in csv["Content-Disposition"]


def test_general_ledger_without_account_shows_form_only(logged_in):
    r = logged_in.get("/reports/general-ledger/")
    assert r.status_code == 200

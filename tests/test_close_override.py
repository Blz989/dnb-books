from datetime import date

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from items.models import MovementKind
from items.stock import StockLine, record_movements, void_movement
from ledger import periods
from ledger.errors import PeriodCloseBlockedError
from ledger.models import AccountingPeriod, EntryStatus
from ledger.periods import close_period
from ledger.posting import void_entry


@pytest.fixture
def blocked_check(monkeypatch):
    checks = list(periods._close_checks)
    monkeypatch.setattr(periods, "_close_checks", checks + [lambda y, m: ["Off by 1.00."]])


def test_override_closes_and_records(admin_user, blocked_check):
    p = close_period(2026, 10, admin_user, override_reason="Known timing difference")
    assert p.is_closed and p.close_override_reason == "Known timing difference"
    assert p.close_override_problems == "Off by 1.00."


def test_blank_reason_still_blocks(admin_user, blocked_check):
    with pytest.raises(PeriodCloseBlockedError, match="Off by 1.00."):
        close_period(2026, 10, admin_user, override_reason="  ")
    assert not AccountingPeriod.objects.filter(year=2026, month=10, is_closed=True).exists()


def test_reason_is_ignored_when_no_problems(admin_user):
    p = close_period(2026, 10, admin_user, override_reason="Not needed")
    assert p.is_closed and p.close_override_reason == "" and p.close_override_problems == ""


def test_standard_user_cannot_override(standard_user, blocked_check):
    with pytest.raises(PermissionDenied):
        close_period(2026, 10, standard_user, override_reason="Please")


def test_admin_close_anyway_page(client, admin_user, blocked_check):
    period = periods.get_period(date(2026, 10, 1))
    client.force_login(admin_user)
    url = reverse("admin:ledger_accountingperiod_close", args=[period.pk])
    page = client.get(url)
    assert page.status_code == 200 and "Off by 1.00." in page.content.decode()
    missing = client.post(url, {"reason": ""})
    assert missing.status_code == 200 and "Enter a reason to close anyway." in missing.content.decode()
    period.refresh_from_db()
    assert not period.is_closed
    done = client.post(url, {"reason": "Known timing difference"})
    assert done.status_code == 302
    period.refresh_from_db()
    assert period.is_closed and period.close_override_reason == "Known timing difference"


def test_blocked_action_offers_close_anyway_link(client, admin_user, blocked_check):
    period = periods.get_period(date(2026, 10, 1))
    client.force_login(admin_user)
    r = client.post(
        reverse("admin:ledger_accountingperiod_changelist"),
        {"action": "close_periods", "_selected_action": [period.pk]}, follow=True,
    )
    body = r.content.decode()
    assert "Off by 1.00." in body and "Close anyway" in body
    assert reverse("admin:ledger_accountingperiod_close", args=[period.pk]) in body


def test_system_void_entry_skips_admin(standard_user, posted_entry):
    with pytest.raises(PermissionDenied):
        void_entry(posted_entry, user=standard_user, reason="x")
    void_entry(posted_entry, user=standard_user, reason="Edited", system=True)
    posted_entry.refresh_from_db()
    assert posted_entry.status == EntryStatus.VOID


def test_system_void_entry_still_needs_reason(standard_user, posted_entry):
    with pytest.raises(ValueError):
        void_entry(posted_entry, user=standard_user, reason=" ", system=True)


def test_system_void_movement_skips_admin(standard_user, admin_user, inv_settings, make_item):
    item = make_item("BOLT-1")
    (m,) = record_movements(
        kind=MovementKind.OPENING, date=date(2026, 10, 1), user=admin_user,
        lines=[StockLine(item, 5, unit_cost=__import__("decimal").Decimal("1.00"))],
    )
    with pytest.raises(PermissionDenied):
        void_movement(m, user=standard_user, reason="x")
    void_movement(m, user=standard_user, reason="Edited", system=True)
    m.refresh_from_db()
    assert m.voided

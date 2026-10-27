"""Spec section 1 success criteria, driven through the test client as an admin."""
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from items.models import Item, MovementKind, StockMovement
from items.stock import check_invariants, on_hand
from ledger.models import JournalEntry

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "items_50.csv"


@pytest.fixture
def admin_client(admin_user):
    c = Client()
    c.force_login(admin_user)
    return c


def test_phase_2_acceptance(
    admin_client, db, inventory_account, cogs_account, revenue, shrinkage_account, writedown_account, opening_equity,
):
    c = admin_client

    # 1. Configure settings, then import 50 items (40 with opening stock).
    r = c.post("/items/settings/", {
        "default_inventory_account": inventory_account.pk, "default_cogs_account": cogs_account.pk,
        "default_income_account": revenue.pk, "shrinkage_account": shrinkage_account.pk,
        "writedown_account": writedown_account.pk, "opening_equity_account": opening_equity.pk}, follow=True)
    assert b"Settings saved." in r.content
    r = c.post("/items/import/", {"file": SimpleUploadedFile("items_50.csv", FIXTURE.read_bytes())})
    assert b"Confirm import" in r.content and b"Fix these rows" not in r.content
    r = c.post("/items/import/confirm/", follow=True)
    assert b"Imported 50 items." in r.content
    assert Item.objects.count() == 50 and StockMovement.objects.filter(kind=MovementKind.OPENING).count() == 40

    # 2. The valuation report ties to the balance sheet's Inventory Asset line.
    valuation = c.get("/reports/inventory-valuation/?as_of=2026-10-31").context["report"]
    assert valuation.difference == D("0.00") and valuation.total > 0 and len(valuation.rows) == 40
    sheet = c.get("/reports/balance-sheet/?as_of=2026-10-31").context["report"]
    inventory_line = next(row.amount for row in sheet.assets if row.account.pk == inventory_account.pk)
    assert valuation.total == inventory_line == valuation.ledger_balance
    assert check_invariants() == []

    # 3. A quantity adjustment, a physical count, and a value adjustment each post and show in the history.
    item = Item.objects.get(sku="ITEM-001")
    start = on_hand(item)
    r = c.post("/items/adjust/", {
        "date": "2026-10-05", "token": str(uuid.uuid4()), "lines-0-sku": "ITEM-001", "lines-0-quantity": "-5",
        "lines-0-reason": "Damaged", "lines-0-note": "", "lines-0-account": ""})
    assert r.status_code == 302 and on_hand(item) == start - 5

    c.post("/items/counts/new/", {"name": "October count", "scope": "ALL", "category": ""})
    from items.models import StockCount

    count = StockCount.objects.get()
    r = c.post(f"/items/counts/{count.pk}/", {f"count-{item.pk}": str(start - 15)})
    assert r.status_code == 302
    r = c.post(f"/items/counts/{count.pk}/review/", {"date": "2026-10-06"})
    assert r.status_code == 302 and on_hand(item) == start - 15

    value_before = c.get("/reports/inventory-valuation/?as_of=2026-10-31").context["report"].total
    r = c.post("/items/ITEM-001/value-adjust/", {
        "date": "2026-10-07", "mode": "change", "amount": "-20.00", "reason": "Faded", "token": str(uuid.uuid4())})
    assert r.status_code == 302

    detail = c.get("/items/ITEM-001/").content.decode()
    for label in ("Opening stock", "Quantity adjustment", "Physical count", "Value adjustment"):
        assert label in detail
    movements = list(StockMovement.objects.filter(item=item).exclude(kind=MovementKind.OPENING))
    assert len(movements) == 3
    for m in movements:
        assert m.journal_entry is not None and m.journal_entry.source_type == "items.movement"
        assert f"JE-{m.journal_entry.pk:06d}" in detail

    # 4. Everything still ties out.
    after = c.get("/reports/inventory-valuation/?as_of=2026-10-31").context["report"]
    assert after.difference == D("0.00") and after.total == value_before - D("20.00")
    assert check_invariants() == []
    assert JournalEntry.objects.filter(source_type="items.movement").count() == 43

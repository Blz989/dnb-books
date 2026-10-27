from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from hypothesis import given, settings as hyp_settings, strategies as st
from hypothesis.extra.django import TestCase as HypothesisTestCase

from core.roles import ADMIN_GROUP
from items.errors import CostingError, ItemStateError, NegativeStockWarning, SettingsMissingError
from items.models import (
    CostingMethod, CostRevision, FifoLayer, Item, ItemType, MovementKind, StockMovement,
)
from items.stock import (
    StockLine, check_invariants, current_unit_cost, on_hand, record_movements, record_value_adjustment,
    replay_item, stock_value, void_movement,
)
from ledger.errors import InvalidLineError, PeriodClosedError
from ledger.models import Account, AccountSubtype, AccountType, EntryStatus
from ledger.periods import close_period

D = Decimal
FIFO = CostingMethod.FIFO
WAVG = CostingMethod.WEIGHTED_AVERAGE
OPENING = MovementKind.OPENING
ADJUSTMENT = MovementKind.ADJUSTMENT


def d(month, day):
    return date(2026, month, day)


def entry_lines(entry):
    return sorted((l.account.number, l.debit, l.credit) for l in entry.lines.select_related("account"))


@pytest.fixture
def item_with_stock(inv_settings, admin_user, make_item):
    item = make_item("BOLT-1")
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    return item


def test_opening_posts_inventory_vs_opening_equity(inv_settings, admin_user, make_item):
    item = make_item("BOLT-1")
    (m,) = record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    assert (m.quantity, m.unit_cost, m.total_cost) == (10, D("1.0000"), D("10.00"))
    assert m.offset_account.number == "3900"
    assert m.journal_entry.status == EntryStatus.POSTED and m.journal_entry.source_type == "items.movement"
    assert m.journal_entry.source_id == str(m.pk)
    assert entry_lines(m.journal_entry) == [("1300", D("10.00"), D("0.00")), ("3900", D("0.00"), D("10.00"))]
    assert on_hand(item) == 10 and stock_value(item) == D("10.00") and check_invariants() == []


def test_adjustment_out_posts_shrinkage(item_with_stock, admin_user):
    (m,) = record_movements(kind=ADJUSTMENT, date=d(10, 5), lines=[StockLine(item_with_stock, -3, reason="Damaged")], user=admin_user)
    assert (m.total_cost, m.unit_cost) == (D("-3.00"), D("1.0000"))
    assert entry_lines(m.journal_entry) == [("1300", D("0.00"), D("3.00")), ("6500", D("3.00"), D("0.00"))]
    assert "Damaged" in m.journal_entry.memo and "BOLT-1" in m.journal_entry.memo
    assert check_invariants() == []


def test_adjustment_in_uses_current_unit_cost_and_chosen_account(item_with_stock, admin_user, expense):
    (m,) = record_movements(kind=ADJUSTMENT, date=d(10, 5), user=admin_user,
                            lines=[StockLine(item_with_stock, 4, account=expense)])
    assert (m.unit_cost, m.total_cost, m.offset_account) == (D("1.0000"), D("4.00"), expense)
    assert entry_lines(m.journal_entry) == [("1300", D("4.00"), D("0.00")), ("6000", D("0.00"), D("4.00"))]


def test_negative_requires_confirmation(item_with_stock, admin_user):
    with pytest.raises(NegativeStockWarning) as e:
        record_movements(kind=ADJUSTMENT, date=d(10, 5), lines=[StockLine(item_with_stock, -12)], user=admin_user)
    assert e.value.messages == ["Line 1: BOLT-1 has 10 on hand. Saving will take it to -2. Continue?"]
    assert StockMovement.objects.filter(kind=ADJUSTMENT).count() == 0
    record_movements(kind=ADJUSTMENT, date=d(10, 5), lines=[StockLine(item_with_stock, -12)], user=admin_user, allow_negative=True)
    assert on_hand(item_with_stock) == -2
    assert check_invariants() == []


def test_two_lines_same_item_checked_cumulatively(item_with_stock, admin_user):
    with pytest.raises(NegativeStockWarning) as e:
        record_movements(kind=ADJUSTMENT, date=d(10, 5), user=admin_user,
                         lines=[StockLine(item_with_stock, -6), StockLine(item_with_stock, -6)])
    assert e.value.messages == ["Line 2: BOLT-1 has 4 on hand. Saving will take it to -2. Continue?"]


def test_example_d_posts_revision_on_outflow_date(inv_settings, admin_user, make_item):
    item = make_item("D-1")
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 2, unit_cost=D("1.00"))], user=admin_user)
    (out,) = record_movements(kind=ADJUSTMENT, date=d(10, 2), lines=[StockLine(item, -5)], user=admin_user, allow_negative=True)
    assert out.total_cost == D("-5.00") and check_invariants() == []
    record_movements(kind=ADJUSTMENT, date=d(10, 20), lines=[StockLine(item, 10, unit_cost=D("1.20"))], user=admin_user)
    rev = CostRevision.objects.get()
    assert rev.delta == D("-0.60") and rev.date == date(2026, 10, 2) and rev.movement == out
    assert rev.journal_entry.source_type == "items.cost_revision" and rev.journal_entry.source_id == str(rev.pk)
    assert entry_lines(rev.journal_entry) == [("1300", D("0.00"), D("0.60")), ("6500", D("0.60"), D("0.00"))]
    assert on_hand(item) == 7 and stock_value(item) == D("8.40")
    assert [(l.remaining_quantity, l.remaining_value) for l in FifoLayer.objects.filter(item=item)] == [(7, D("8.40"))]
    assert check_invariants() == []


def test_example_e_backdated_inflow_revision(inv_settings, admin_user, make_item):
    item = make_item("E-1")
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    (out,) = record_movements(kind=ADJUSTMENT, date=d(10, 10), lines=[StockLine(item, -5)], user=admin_user)
    record_movements(kind=ADJUSTMENT, date=d(9, 25), lines=[StockLine(item, 10, unit_cost=D("0.80"))], user=admin_user)
    rev = CostRevision.objects.get()
    assert rev.delta == D("1.00") and rev.date == d(10, 10) and rev.movement == out
    assert stock_value(item) == D("14.00") and on_hand(item) == 15
    assert check_invariants() == []


def test_revision_dated_on_trigger_when_outflow_month_closed(inv_settings, admin_user, make_item):
    item = make_item("E-2")
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJUSTMENT, date=d(10, 10), lines=[StockLine(item, -5)], user=admin_user)
    close_period(2026, 10, admin_user)
    record_movements(kind=ADJUSTMENT, date=d(9, 25), lines=[StockLine(item, 10, unit_cost=D("0.80"))], user=admin_user)
    assert CostRevision.objects.get().date == d(9, 25)
    assert check_invariants() == []


def test_movement_in_closed_month_is_blocked_even_at_zero_cost(inv_settings, admin_user, make_item):
    item = make_item("Z-1", purchase_cost="0")
    close_period(2026, 10, admin_user)
    with pytest.raises(PeriodClosedError):
        record_movements(kind=ADJUSTMENT, date=d(10, 5), lines=[StockLine(item, 5)], user=admin_user)
    assert StockMovement.objects.count() == 0


def test_weighted_average_outflow_uses_average(inv_settings, admin_user, make_item):
    item = make_item("W-1", method=WAVG)
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_movements(kind=ADJUSTMENT, date=d(10, 2), lines=[StockLine(item, 10, unit_cost=D("1.50"))], user=admin_user)
    (out,) = record_movements(kind=ADJUSTMENT, date=d(10, 3), lines=[StockLine(item, -15)], user=admin_user)
    assert out.total_cost == D("-18.75") and out.unit_cost == D("1.2500")
    assert stock_value(item) == D("6.25") and current_unit_cost(item) == D("1.2500")
    assert check_invariants() == []


def test_settings_missing_message(admin_user, make_item):  # Review Focus 1
    item = make_item("S-1")
    with pytest.raises(SettingsMissingError, match="Set up inventory accounts first."):
        record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 1, unit_cost=D("1.00"))], user=admin_user)
    assert StockMovement.objects.count() == 0


def test_backdate_breaking_value_adjustment_rolls_back(inv_settings, admin_user, make_item):  # Review Focus 3
    item = make_item("V-1")
    record_movements(kind=OPENING, date=d(10, 1), lines=[StockLine(item, 10, unit_cost=D("1.00"))], user=admin_user)
    record_value_adjustment(item=item, date=d(10, 5), amount=D("-2.00"), reason="Damaged", user=admin_user)
    with pytest.raises(CostingError, match="Oct 5, 2026"):
        record_movements(kind=ADJUSTMENT, date=d(10, 3), lines=[StockLine(item, -10)], user=admin_user)
    assert StockMovement.objects.count() == 2 and CostRevision.objects.count() == 0
    assert check_invariants() == []


def test_zero_cost_item_posts_no_entry(inv_settings, admin_user, make_item):  # Review Focus 5
    item = make_item("ZERO-1", purchase_cost="0")
    (m,) = record_movements(kind=ADJUSTMENT, date=d(10, 1), lines=[StockLine(item, 5)], user=admin_user)
    assert m.journal_entry is None and m.total_cost == D("0.00")
    (out,) = record_movements(kind=ADJUSTMENT, date=d(10, 2), lines=[StockLine(item, -2)], user=admin_user)
    assert out.journal_entry is None and on_hand(item) == 3 and check_invariants() == []


def test_value_adjustment_posts_writedown_and_writeup(item_with_stock, admin_user):
    m = record_value_adjustment(item=item_with_stock, date=d(10, 5), amount=D("-2.00"), reason="Faded", user=admin_user)
    assert (m.quantity, m.total_cost, m.kind) == (0, D("-2.00"), MovementKind.VALUE_ADJUSTMENT)
    assert entry_lines(m.journal_entry) == [("1300", D("0.00"), D("2.00")), ("6510", D("2.00"), D("0.00"))]
    up = record_value_adjustment(item=item_with_stock, date=d(10, 6), amount=D("0.50"), reason="Repriced", user=admin_user)
    assert entry_lines(up.journal_entry) == [("1300", D("0.50"), D("0.00")), ("6510", D("0.00"), D("0.50"))]
    assert stock_value(item_with_stock) == D("8.50") and check_invariants() == []


def test_value_adjustment_blocked_at_zero_stock(inv_settings, admin_user, make_item):
    item = make_item("V-2")
    with pytest.raises(ItemStateError, match="V-2 has no stock to revalue."):
        record_value_adjustment(item=item, date=d(10, 5), amount=D("1.00"), reason="x", user=admin_user)


def test_value_adjustment_admin_only(standard_user, item_with_stock):
    with pytest.raises(PermissionDenied):
        record_value_adjustment(item=item_with_stock, date=d(10, 5), amount=D("1.00"), reason="x", user=standard_user)


def test_non_stock_and_inactive_items_rejected(inv_settings, admin_user, make_item, revenue):
    ns = Item.objects.create(sku="NS-1", name="Service-ish", item_type=ItemType.NON_STOCK, income_account=revenue)
    with pytest.raises(ItemStateError, match="Line 1: NS-1 is not an active stock item."):
        record_movements(kind=ADJUSTMENT, date=d(10, 1), lines=[StockLine(ns, 1)], user=admin_user)
    inactive = make_item("OFF-1", is_active=False)
    with pytest.raises(ItemStateError, match="Line 1: OFF-1 is not an active stock item."):
        record_movements(kind=ADJUSTMENT, date=d(10, 1), lines=[StockLine(inactive, 1)], user=admin_user)


def test_zero_quantity_rejected(item_with_stock, admin_user):
    with pytest.raises(ItemStateError, match="Line 1: enter a quantity other than zero."):
        record_movements(kind=ADJUSTMENT, date=d(10, 1), lines=[StockLine(item_with_stock, 0)], user=admin_user)


def test_failed_line_saves_nothing(item_with_stock, admin_user, inventory_account):
    # Offset account that is itself an inventory account is refused by the ledger rule? No: use a missing-stock second line.
    with pytest.raises(ItemStateError):
        record_movements(kind=ADJUSTMENT, date=d(10, 5), user=admin_user,
                         lines=[StockLine(item_with_stock, -1), StockLine(item_with_stock, 0)])
    assert StockMovement.objects.filter(kind=ADJUSTMENT).count() == 0


def test_check_invariants_reports_drift(item_with_stock, admin_user, cash, inventory_account):
    from ledger.posting import LineSpec, post_entry

    post_entry(date=d(10, 2), user=admin_user, source_type="items.movement", source_id="999", lines=[
        LineSpec(inventory_account, debit=D("1.00")), LineSpec(cash, credit=D("1.00"))])
    assert check_invariants() == ["1300 Inventory Asset: stock says 10.00, ledger says 11.00."]


def test_replay_item_is_idempotent(item_with_stock, admin_user):
    record_movements(kind=ADJUSTMENT, date=d(10, 2), lines=[StockLine(item_with_stock, -3)], user=admin_user)
    assert replay_item(item_with_stock, trigger_date=d(10, 3), user=admin_user) == []


class RandomOperationsKeepInvariants(HypothesisTestCase):
    def setUp(self):
        call_command("setup_roles", verbosity=0)
        self.user = get_user_model().objects.create_user("rnd", password="pw")
        self.user.groups.add(Group.objects.get(name=ADMIN_GROUP))

        def acct(n, name, t, s):
            return Account.objects.create(number=n, name=name, type=t, subtype=s)

        self.inv = acct("1300", "Inventory Asset", AccountType.ASSET, AccountSubtype.INVENTORY)
        cogs = acct("5000", "COGS", AccountType.EXPENSE, AccountSubtype.COST_OF_GOODS_SOLD)
        rev = acct("4000", "Sales", AccountType.INCOME, AccountSubtype.INCOME)
        from items.models import InventorySettings

        s = InventorySettings.load()
        s.default_inventory_account, s.default_cogs_account, s.default_income_account = self.inv, cogs, rev
        s.shrinkage_account = acct("6500", "Shrink", AccountType.EXPENSE, AccountSubtype.EXPENSE)
        s.writedown_account = acct("6510", "Writedown", AccountType.EXPENSE, AccountSubtype.EXPENSE)
        s.opening_equity_account = acct("3900", "OBE", AccountType.EQUITY, AccountSubtype.EQUITY)
        s.save()
        self.items = [
            Item.objects.create(sku=f"R-{i}", name="r", item_type=ItemType.STOCK, costing_method=m,
                                income_account=rev, cogs_account=cogs, inventory_account=self.inv,
                                purchase_cost=D("1.00"))
            for i, m in enumerate([FIFO, WAVG])
        ]

    @given(ops=st.lists(
        st.tuples(st.integers(0, 1), st.sampled_from(["in", "out", "adj", "void"]), st.integers(1, 20),
                  st.integers(1, 28), st.integers(1, 3000)),
        min_size=1, max_size=12))
    @hyp_settings(max_examples=40, deadline=None)
    def test_random_operations_keep_invariants(self, ops):
        for idx, kind, qty, day, cents in ops:
            item = self.items[idx]
            try:
                if kind == "in":
                    record_movements(kind=ADJUSTMENT, date=d(10, day), user=self.user,
                                     lines=[StockLine(item, qty, unit_cost=D(cents) / D(1000))])
                elif kind == "out":
                    record_movements(kind=ADJUSTMENT, date=d(10, day), user=self.user,
                                     lines=[StockLine(item, -qty)], allow_negative=True)
                elif kind == "adj":
                    record_value_adjustment(item=item, date=d(10, day), amount=D(cents) / D(100) - D("5.00") or D("0.01"),
                                            reason="r", user=self.user)
                else:
                    m = StockMovement.objects.filter(item=item, voided=False).order_by("-id").first()
                    if m:
                        void_movement(m, user=self.user, reason="r", date=d(10, 28))
            except (CostingError, ItemStateError):
                continue
            assert check_invariants() == []

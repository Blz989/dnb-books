"""purchasing 0003: the one-to-one PO line link becomes allocations, and back."""
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from contacts.models import Customer, Vendor
from purchasing.models import PurchaseOrder, PurchaseOrderAllocation
from sales.models import SalesOrder, SalesOrderLine
from tests.test_dropship_regression import scenario  # noqa: F401  (fixture)

D = Decimal
BEFORE = [("purchasing", "0002_seed_thresholds")]
AFTER = [("purchasing", "0003_po_allocations")]


def migrate(target):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(target)
    return executor.loader.project_state(target).apps


@pytest.fixture
def old_schema(transactional_db):
    """Leaves the database at 0002 for the test, and back at the latest migration afterwards."""
    apps = migrate(BEFORE)
    yield apps
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture
def purge_posted():
    """Like ledger_purge and items_purge, for a test that leaves bills pointing at posted entries: foreign keys
    are off while the posted rows go, and Django's flush removes the rest."""
    import importlib

    yield
    migs = [importlib.import_module(f"{app}.migrations.0002_immutability_triggers") for app in ("ledger", "items")]
    with connection.cursor() as c:
        c.execute("PRAGMA foreign_keys = OFF")
        for mig in migs:
            for sql in mig.REVERSE:
                c.execute(sql)
        for table in ("items_costrevision", "items_fifolayer", "items_stockmovement", "ledger_journalline",
                      "ledger_journalentry"):
            c.execute(f"DELETE FROM {table}")
        for mig in migs:
            for sql in mig.FORWARD:
                c.execute(sql)
        c.execute("PRAGMA foreign_keys = ON")


def _order(user, customer, number, item, qty, day):
    order = SalesOrder.objects.create(number=number, customer=customer, date=day, created_by=user)
    line = SalesOrderLine.objects.create(order=order, item=item, quantity=qty, price=D("9.50"),
                                         fulfillment="DROP_SHIP", drop_ship_vendor="Acme Mfg")
    return order, line


def test_forward_makes_one_allocation_per_link(old_schema, make_item, admin_user):
    OldPO = old_schema.get_model("purchasing", "PurchaseOrder")
    OldLine = old_schema.get_model("purchasing", "PurchaseOrderLine")
    customer = Customer.objects.create(name="Acme Builders")
    vendor = Vendor.objects.create(name="Acme Mfg")
    gadget = make_item("GADGET")
    so1, so1_line = _order(admin_user, customer, "SO-0001", gadget, 3, date(2026, 10, 1))
    so2, so2_line = _order(admin_user, customer, "SO-0002", gadget, 4, date(2026, 10, 2))

    linked = OldPO.objects.create(number="PO-0001", vendor_id=vendor.pk, date=date(2026, 10, 2),
                                  sales_order_id=so1.pk, created_by_id=admin_user.pk, notes="Rush")
    # The vendor shipped 5, so the PO line was raised above the order line's 3.
    OldLine.objects.create(order=linked, item_id=gadget.pk, quantity=5, price=D("4.00"), so_line_id=so1_line.pk)
    OldLine.objects.create(order=linked, item_id=gadget.pk, quantity=2, price=D("4.00"))
    # Edited before this change, which dropped its line link but kept the order.
    unlinked = OldPO.objects.create(number="PO-0002", vendor_id=vendor.pk, date=date(2026, 10, 3),
                                    sales_order_id=so2.pk, created_by_id=admin_user.pk)
    OldLine.objects.create(order=unlinked, item_id=gadget.pk, quantity=4, price=D("4.00"))
    stock = OldPO.objects.create(number="PO-0003", vendor_id=vendor.pk, date=date(2026, 10, 3),
                                 created_by_id=admin_user.pk)
    OldLine.objects.create(order=stock, item_id=gadget.pk, quantity=9, price=D("4.00"))

    migrate(AFTER)

    (a,) = PurchaseOrderAllocation.objects.all()
    assert (a.po_line.order.number, a.po_line.quantity, a.so_line_id, a.quantity) == ("PO-0001", 5, so1_line.pk, 5)
    po1, po2, po3 = PurchaseOrder.objects.order_by("number")
    assert po1.sales_orders() == [so1] and po1.is_drop_ship and po1.notes == "Rush"
    assert po2.sales_orders() == [] and po2.notes == "Drop ship for SO-0002."
    assert po3.sales_orders() == [] and po3.notes == "" and not po3.is_drop_ship


def test_reverse_restores_links_and_refuses_shared_lines(old_schema, make_item, admin_user):
    migrate(AFTER)
    customer = Customer.objects.create(name="Acme Builders")
    vendor = Vendor.objects.create(name="Acme Mfg")
    gadget = make_item("GADGET")
    so1, so1_line = _order(admin_user, customer, "SO-0001", gadget, 3, date(2026, 10, 1))
    so2, so2_line = _order(admin_user, customer, "SO-0002", gadget, 4, date(2026, 10, 2))
    po = PurchaseOrder.objects.create(number="PO-0001", vendor=vendor, date=date(2026, 10, 2), created_by=admin_user)
    line = po.lines.create(item=gadget, quantity=3, price=D("4.00"))
    PurchaseOrderAllocation.objects.create(po_line=line, so_line=so1_line, quantity=3)

    apps = migrate(BEFORE)
    old_line = apps.get_model("purchasing", "PurchaseOrderLine").objects.get()
    old_po = apps.get_model("purchasing", "PurchaseOrder").objects.get()
    assert (old_line.so_line_id, old_po.sales_order_id) == (so1_line.pk, so1.pk)

    migrate(AFTER)
    line = PurchaseOrder.objects.get().lines.get()
    assert [(a.so_line_id, a.quantity) for a in line.allocations.all()] == [(so1_line.pk, 3)]
    line.quantity = 7
    line.save()
    PurchaseOrderAllocation.objects.create(po_line=line, so_line=so2_line, quantity=4)
    with pytest.raises(RuntimeError, match="A purchase order line is shared by several sales orders. "
                                           "Undo the sharing first."):
        migrate(BEFORE)


def test_round_trip_keeps_every_number(transactional_db, scenario, purge_posted):
    """Single-link data back to the old schema and forward again: the rebuilt matching and the profit replay
    give exactly the numbers recorded before shared POs existed."""
    from purchasing import dropship
    from sales.models import InvoiceLine
    from tests.test_dropship_regression import EXPECTED, snapshot

    assert snapshot() == EXPECTED
    try:
        migrate(BEFORE)
    finally:
        migrate(AFTER)
    assert PurchaseOrderAllocation.objects.count() == 3
    dropship.rebuild(InvoiceLine.objects.filter(so_line__isnull=False).values_list("so_line_id", flat=True))
    assert snapshot() == EXPECTED

"""Demo catalog for seed_demo: items, opening stock, adjustments, one item below zero."""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from ledger.models import Account

from .models import Category, CostingMethod, InventorySettings, Item, ItemType, MovementKind
from . import counts
from .stock import StockLine, record_movements, record_value_adjustment

D = Decimal
FIFO, AVG = CostingMethod.FIFO, CostingMethod.WEIGHTED_AVERAGE

# sku, name, category, unit, method, cost, price, opening qty, vendor part
STOCK = [
    ("BOLT-14", 'Hex bolt 1/4" zinc', "Fasteners", "each", FIFO, "0.4950", "0.89", 980, "HB-014"),
    ("BOLT-38", 'Hex bolt 3/8" zinc', "Fasteners", "each", FIFO, "0.7200", "1.29", 412, "HB-038"),
    ("NUT-14", 'Hex nut 1/4" zinc', "Fasteners", "each", FIFO, "0.1200", "0.29", 1500, "HN-014"),
    ("WASH-14", 'Flat washer 1/4"', "Fasteners", "each", FIFO, "0.0400", "0.12", 2200, "FW-014"),
    ("SCRW-8-1", "Wood screw #8 x 1in, box 100", "Fasteners", "box", AVG, "3.2000", "5.99", 64, "WS-081"),
    ("SCRW-10-2", "Deck screw #10 x 2in, box 100", "Fasteners", "box", AVG, "4.1500", "7.49", 48, "DS-102"),
    ("NAIL-16D", "Framing nail 16d, 5 lb", "Fasteners", "box", FIFO, "6.8000", "11.99", 30, "FN-16"),
    ("ANCH-38", 'Wedge anchor 3/8"', "Fasteners", "each", FIFO, "0.9500", "1.79", 240, "WA-038"),
    ("PNT-RED-QT", "Red enamel, quart", "Paint", "each", AVG, "7.2000", "13.99", 12, "RE-Q"),
    ("PNT-WHT-GL", "White latex, gallon", "Paint", "each", AVG, "18.4375", "32.99", 64, "WL-G"),
    ("PNT-BLK-QT", "Black enamel, quart", "Paint", "each", AVG, "7.4000", "13.99", 18, "BE-Q"),
    ("PNT-PRM-GL", "Primer, gallon", "Paint", "each", AVG, "14.2500", "24.99", 36, "PR-G"),
    ("PNT-BRSH-2", 'Brush 2" angled', "Paint", "each", FIFO, "3.1000", "6.49", 55, "BR-2"),
    ("TAPE-BL-2", 'Blue painter\'s tape 2"', "Supplies", "roll", FIFO, "3.1000", "5.99", 210, "BT-2"),
    ("TAPE-DUCT", "Duct tape 60 yd", "Supplies", "roll", FIFO, "4.2500", "7.99", 90, "DT-60"),
    ("GLOVE-NIT-L", "Nitrile gloves, large, box", "Supplies", "box", AVG, "11.2340", "17.99", 47, "NG-L"),
    ("GLOVE-NIT-M", "Nitrile gloves, medium, box", "Supplies", "box", AVG, "11.2340", "17.99", 52, "NG-M"),
    ("TARP-8X10", "Poly tarp 8 x 10 ft", "Supplies", "each", FIFO, "5.5000", "9.99", 26, "TP-810"),
    ("ZIP-14", 'Cable ties 14", bag 100', "Supplies", "bag", FIFO, "4.8000", "8.49", 70, "CT-14"),
    ("WIRE-12-250", "12 AWG wire, 250 ft spool", "Electrical", "spool", FIFO, "96.5000", "139.00", 18, "W12-250"),
    ("WIRE-14-250", "14 AWG wire, 250 ft spool", "Electrical", "spool", FIFO, "72.0000", "104.00", 14, "W14-250"),
    ("OUTLET-15", "Duplex outlet 15A, white", "Electrical", "each", FIFO, "0.8500", "2.29", 320, "OD-15"),
    ("SWITCH-SP", "Single pole switch, white", "Electrical", "each", FIFO, "0.9500", "2.49", 280, "SW-SP"),
    ("BOX-1G", "Old-work box, 1 gang", "Electrical", "each", FIFO, "0.6500", "1.79", 400, "OB-1"),
    ("BREAK-20", "Circuit breaker 20A", "Electrical", "each", AVG, "6.2500", "11.99", 40, "CB-20"),
    ("LAMP-A19", "LED bulb A19, 4 pack", "Electrical", "pack", AVG, "5.1000", "8.99", 75, "LB-A19"),
    ("PIPE-PVC-12", 'PVC pipe 1/2" x 10 ft', "Plumbing", "each", FIFO, "2.1000", "4.29", 120, "PVC-12"),
    ("PIPE-PVC-34", 'PVC pipe 3/4" x 10 ft', "Plumbing", "each", FIFO, "3.0500", "5.99", 95, "PVC-34"),
    ("ELB-PVC-12", 'PVC elbow 1/2"', "Plumbing", "each", FIFO, "0.2800", "0.79", 600, "PE-12"),
    ("VALVE-BALL-12", 'Ball valve 1/2" brass', "Plumbing", "each", FIFO, "5.9000", "11.49", 60, "BV-12"),
    ("TAPE-PTFE", "PTFE thread tape", "Plumbing", "roll", FIFO, "0.6000", "1.69", 350, "PT-1"),
    ("CEMENT-PVC", "PVC cement, 8 oz", "Plumbing", "each", AVG, "4.3000", "7.99", 44, "PC-8"),
    ("FAUCET-KIT", "Kitchen faucet, chrome", "Plumbing", "each", FIFO, "38.0000", "69.00", 9, "FK-C"),
    ("HOSE-50", "Garden hose 50 ft", "Plumbing", "each", FIFO, "14.5000", "26.99", 22, "GH-50"),
    ("CAULK-CLR", "Silicone caulk, clear", "Plumbing", "each", AVG, "3.7500", "6.99", 88, "SC-C"),
]
NON_STOCK = [
    ("SO-PART", "Special order part", None),
    ("DELIVERY", "Delivery charge", None),
    ("CUT-FEE", "Cutting fee", None),
]


def seed_items(user):
    s = InventorySettings.load()
    by_number = {a.number: a for a in Account.objects.all()}
    s.default_inventory_account = by_number["1300"]
    s.default_cogs_account = by_number["5000"]
    s.default_income_account = by_number["4000"]
    s.shrinkage_account = by_number["6500"]
    s.writedown_account = by_number["6510"]
    s.opening_equity_account = by_number["3900"]
    s.save()

    today = timezone.localdate()
    first = today.replace(day=1)
    opening_day = first - timedelta(days=45)
    categories = {n: Category.objects.create(name=n) for n in ("Fasteners", "Paint", "Supplies", "Electrical", "Plumbing")}

    items = {}
    lines = []
    for sku, name, cat, unit, method, cost, price, qty, vendor in STOCK:
        item = Item.objects.create(
            sku=sku, name=name, category=categories[cat], unit=unit, costing_method=method,
            purchase_cost=D(cost), sale_price=D(price), vendor_part_number=vendor, item_type=ItemType.STOCK,
            income_account=s.default_income_account, cogs_account=s.default_cogs_account,
            inventory_account=s.default_inventory_account)
        items[sku] = item
        lines.append(StockLine(item, qty, unit_cost=D(cost)))
    for sku, name, cat in NON_STOCK:
        Item.objects.create(sku=sku, name=name, item_type=ItemType.NON_STOCK, income_account=s.default_income_account)
    record_movements(kind=MovementKind.OPENING, date=opening_day, lines=lines, user=user)

    adj = MovementKind.ADJUSTMENT
    record_movements(kind=adj, date=first - timedelta(days=20), user=user, lines=[
        StockLine(items["BOLT-14"], 400, reason="Receipt", unit_cost=D("0.5200")),
        StockLine(items["PNT-WHT-GL"], 24, reason="Receipt", unit_cost=D("19.0000"))])
    record_movements(kind=adj, date=first - timedelta(days=12), user=user, lines=[
        StockLine(items["PNT-RED-QT"], -4, reason="Damaged: dented cans"),
        StockLine(items["GLOVE-NIT-L"], -2, reason="Lost"),
        StockLine(items["TAPE-BL-2"], -6, reason="Sample")])
    record_value_adjustment(item=items["FAUCET-KIT"], date=first - timedelta(days=9), amount=D("-36.00"),
                            reason="Finish scratched on 3 units", user=user)
    # Below zero: the demo's example of an estimated cost.
    record_movements(kind=adj, date=first - timedelta(days=5), user=user, allow_negative=True,
                     lines=[StockLine(items["PNT-RED-QT"], -11, reason="Correction")])
    # Backdated inflow after later outflows, so one cost revision shows in the history.
    record_movements(kind=adj, date=opening_day - timedelta(days=5), user=user, lines=[
        StockLine(items["TAPE-BL-2"], 40, reason="Late-entered receipt", unit_cost=D("2.9000"))])
    return items


def seed_counts(user, items):
    """One posted count and one open count with a flagged item."""
    today = timezone.localdate()
    first = today.replace(day=1)

    supplies = Category.objects.get(name="Supplies")
    done = counts.start_count(name="September supplies count", category=supplies, user=user)
    counts.enter_counts(done, {items["GLOVE-NIT-M"].pk: 50, items["TAPE-DUCT"].pk: 90, items["ZIP-14"].pk: 70}, user)
    counts.post_count(done, date=first - timedelta(days=2), user=user)

    open_count = counts.start_count(name="October full count", category=None, user=user)
    # Stock moves after the snapshot, so this item shows as flagged on the review screen.
    record_movements(kind=MovementKind.ADJUSTMENT, date=today, user=user,
                     lines=[StockLine(items["TAPE-BL-2"], -2, reason="Sample")])
    counts.enter_counts(open_count, {
        items["NAIL-16D"].pk: 28, items["WIRE-12-250"].pk: 18, items["HOSE-50"].pk: 21,
        items["TAPE-BL-2"].pk: 241, items["BOLT-38"].pk: 412, items["NUT-14"].pk: 1500,
        items["FAUCET-KIT"].pk: 9, items["VALVE-BALL-12"].pk: 59,
    }, user)

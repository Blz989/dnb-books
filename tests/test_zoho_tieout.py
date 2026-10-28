import csv
from datetime import date
from decimal import Decimal

import pytest
from django.db.models import Sum

from items.models import Item, ItemType
from items.stock import on_hand, stock_value
from ledger.models import Account, AccountType, JournalLine
from migration.zoho import mapping
from migration.zoho.reader import read_export
from migration.zoho.replay import Replay
from migration.tieout import Check, Diff, TieOut, compare, render_html, render_pdf
from purchasing.reports import ap_aging
from sales.reports import ar_aging
from tests.zoho_sample import build_sample_export

D = Decimal
AS_OF = date(2026, 12, 31)


@pytest.fixture
def run(db, admin_user, tmp_path):
    export = read_export(build_sample_export(tmp_path))
    replay = Replay(export, admin_user)
    replay.load_master()
    for event in replay.events():
        replay.post(event)
    after = replay.finish()
    plan = mapping.account_plan(export.files["accounts"])
    return export, replay, after, plan


def _write(path, columns, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)


def _balance(account, **filters):
    agg = JournalLine.objects.in_ledger().filter(account=account, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or D("0.00")) - (agg["c"] or D("0.00"))


def make_reports(folder, plan, years=(2025,)):
    """Normalized reports built from the replayed ledger's own figures."""
    folder.mkdir(exist_ok=True)
    tb, pl = [], {y: [] for y in years}
    for p in plan:
        account = Account.objects.get(number=p.number)
        bal = _balance(account, entry__date__lte=AS_OF)
        tb.append([p.name, bal if bal > 0 else "0.00", -bal if bal < 0 else "0.00"])
        if account.type in (AccountType.INCOME, AccountType.EXPENSE):
            for y in years:
                act = _balance(account, entry__date__year=y)
                pl[y].append([p.name, -act if account.type == AccountType.INCOME else act])
    _write(folder / "trial_balance.csv", ["account", "debit", "credit"], tb)
    for y, rows in pl.items():
        _write(folder / f"pl_{y}.csv", ["account", "amount"], rows)
    _write(folder / "ar_aging.csv", ["total"], [[ar_aging(AS_OF).totals.total]])
    _write(folder / "ap_aging.csv", ["total"], [[ap_aging(AS_OF).totals.total]])
    items = Item.objects.filter(item_type=ItemType.STOCK)
    _write(folder / "inventory_summary.csv", ["sku", "quantity"], [[i.sku, on_hand(i)] for i in items])
    _write(folder / "inventory_valuation.csv", ["sku", "value"], [[i.sku, stock_value(i)] for i in items])
    return folder


def run_compare(run, folder):
    export, replay, after, plan = run
    return compare(folder, AS_OF, after, plan, replay.warnings, export)


def by_name(t, name):
    return next(c for c in t.checks if c.name == name)


def test_matching_reports_pass(run, tmp_path):
    t = run_compare(run, make_reports(tmp_path / "reports", run[3]))
    assert isinstance(t, TieOut) and all(isinstance(c, Check) for c in t.checks)
    assert [c.name for c in t.checks if not c.passed] == []
    assert t.passed
    assert by_name(t, "Trial balance").rows  # real rows were compared
    assert by_name(t, "Profit and loss 2025").rows
    assert any(r.label == "Net income" for r in by_name(t, "Profit and loss 2025").rows)
    assert all(r.ours == r.zoho for c in t.checks for r in c.rows)
    # document counts and totals line up with the export
    assert t.counts and all(isinstance(d, Diff) for d in t.counts)
    labels = [d.label for d in t.counts]
    for expected in ("Invoices: count", "Invoices: total", "Bills: count", "Payments: count",
                     "Vendor payments: count", "Credit notes: count", "Vendor credits: count"):
        assert expected in labels
    assert [d.label for d in t.counts if d.ours != d.zoho] == []
    assert next(d for d in t.counts if d.label == "Invoices: count").ours == 9
    html = render_html(t)
    assert "Passed" in html and "Failed" not in html


def test_wrong_trial_balance_account_fails_and_is_named(run, tmp_path):
    folder = make_reports(tmp_path / "reports", run[3])
    rows = list(csv.reader(open(folder / "trial_balance.csv")))
    target = next(r for r in rows[1:] if r[0] == "Sales")
    target[1] = str(D(target[1]) + D("10.00"))
    _write(folder / "trial_balance.csv", rows[0], rows[1:])
    t = run_compare(run, folder)
    tb = by_name(t, "Trial balance")
    assert not tb.passed and not t.passed
    assert tb.rows[0].label == "Sales"  # failing rows first
    assert tb.rows[0].zoho - tb.rows[0].ours == D("10.00")
    assert [r.label for r in tb.rows if r.ours != r.zoho] == ["Sales"]
    assert by_name(t, "Profit and loss 2025").passed
    html = render_html(t)
    assert "Failed" in html and "Sales" in html


def test_missing_report_not_provided(run, tmp_path):
    folder = make_reports(tmp_path / "reports", run[3])
    (folder / "ar_aging.csv").unlink()
    t = run_compare(run, folder)
    ar = by_name(t, "A/R aging")
    assert ar.passed is False and ar.provided is False and ar.rows == []
    assert not t.passed
    assert by_name(t, "A/P aging").passed
    assert "Not provided" in render_html(t)
    empty = run_compare(run, tmp_path / "nothing_here")
    assert not empty.passed
    assert all(not c.passed and not c.provided for c in empty.checks)
    names = [c.name for c in empty.checks]
    for n in ("Trial balance", "Profit and loss", "A/R aging", "A/P aging", "Inventory quantities"):
        assert n in names


def test_inventory_value_listed_not_failed(run, tmp_path):
    folder = make_reports(tmp_path / "reports", run[3])
    rows = list(csv.reader(open(folder / "inventory_valuation.csv")))
    for r in rows[1:]:
        r[1] = str(D(r[1]) + D("1.00"))
    _write(folder / "inventory_valuation.csv", rows[0], rows[1:])
    t = run_compare(run, folder)
    assert t.passed
    assert {d.label for d in t.inventory_value} == {r[0] for r in rows[1:]}
    assert all(d.zoho - d.ours == D("1.00") for d in t.inventory_value)
    notes = {d.label: d.note for d in t.inventory_value}
    assert notes["BOLT-D"] == "ever negative"  # on hand went below zero during the replay
    assert notes["WID-A"] == ""
    assert "BOLT-D" in render_html(t)


def test_pdf_renders(run, tmp_path):
    folder = make_reports(tmp_path / "reports", run[3])
    t = run_compare(run, folder)
    assert render_pdf(t)[:4] == b"%PDF"
    (folder / "trial_balance.csv").unlink()
    assert render_pdf(run_compare(run, folder))[:4] == b"%PDF"


def test_html_is_standalone_with_theme_toggle_and_sections(run, tmp_path):
    t = run_compare(run, make_reports(tmp_path / "reports", run[3]))
    html = render_html(t)
    assert "data-theme-choice" in html and "--surface" in html and 'data-theme="dark"' in html
    order = ["Summary", "Inventory value", "Negative items", "Undeposited by year", "Unbilled orders",
             "Warnings", "Account number map"]
    positions = [html.index(f"<h2>{s}</h2>") for s in order]
    assert positions == sorted(positions)
    assert positions[0] < html.index("Trial balance") < positions[1]

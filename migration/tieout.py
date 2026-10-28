"""Tie-out: compares the replayed ledger with Zoho's own reports.

The reports folder holds normalized CSVs (see compare). Every figure on the DNB side comes from posted ledger
lines (`JournalLine.objects.in_ledger()`), the aging reports, or the stock functions, never from documents,
except the document counts, which are compared with the export on purpose."""
import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db.models import Sum
from django.template.loader import render_to_string
from weasyprint import CSS, HTML

from items.models import Item, ItemType
from items.stock import on_hand, stock_value
from ledger.models import Account, AccountType, JournalLine
from migration.models import ZohoRecord
from migration.zoho import mapping
from purchasing.balances import bill_total
from purchasing.balances import credit_total as vendor_credit_total
from purchasing.reports import ap_aging
from sales.balances import credit_total, invoice_total
from sales.reports import ar_aging

ZERO = Decimal("0.00")
CSS_PATH = Path(settings.BASE_DIR) / "migration" / "static" / "migration" / "tieout.css"
FONT_DIR = Path(settings.BASE_DIR) / "core" / "static" / "core" / "fonts"
NEGATIVE_WARNING = re.compile(r"^(.*): stock went below zero on ")


@dataclass
class Diff:
    label: str
    ours: Decimal
    zoho: Decimal
    note: str = ""

    @property
    def difference(self) -> Decimal:
        return self.zoho - self.ours

    @property
    def matches(self) -> bool:
        return self.ours == self.zoho


@dataclass
class Check:
    name: str
    passed: bool
    rows: list[Diff] = field(default_factory=list)
    provided: bool = True  # False when the report file is missing: reads "Not provided" and counts as failed

    @property
    def failing(self) -> int:
        return sum(1 for r in self.rows if not r.matches)


@dataclass
class TieOut:
    checks: list[Check]
    inventory_value: list[Diff]
    counts: list[Diff]
    after: object  # AfterSummary
    account_map: list  # AccountPlan
    warnings: list[str]
    as_of: date | None = None
    inventory_value_provided: bool = True

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def count_differences(self) -> int:
        return sum(1 for d in self.counts if not d.matches)


# ---- reading ----
def _read(path: Path) -> list[dict] | None:
    if not path.is_file():
        return None
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def _check(name: str, rows: list[Diff] | None) -> Check:
    if rows is None:
        return Check(name, False, [], provided=False)
    rows = sorted(rows, key=lambda r: (r.matches, r.label.casefold()))  # failing rows first
    return Check(name, all(r.matches for r in rows), rows)


def _net(account: Account, **filters) -> Decimal:
    agg = JournalLine.objects.in_ledger().filter(account=account, **filters).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


# ---- the checks ----
def _trial_balance(rows, plan, as_of) -> list[Diff]:
    by_name = {p.name.casefold(): p for p in plan}
    zoho: dict[str, Decimal] = defaultdict(lambda: ZERO)
    names: dict[str, str] = {}
    for row in rows:
        key = row["account"].casefold()
        names.setdefault(key, row["account"])
        zoho[key] += mapping.money(row.get("debit", "")) - mapping.money(row.get("credit", ""))
    ours: dict[str, Decimal] = {}
    notes: dict[str, str] = {}
    mapped_numbers = set()
    for key in zoho:
        if key in by_name:
            mapped_numbers.add(by_name[key].number)
            account = Account.objects.get(number=by_name[key].number)
            ours[key] = _net(account, entry__date__lte=as_of)
        else:
            ours[key], notes[key] = ZERO, "Not in the account map"
    # Accounts that hold a balance here but are missing from Zoho's report
    for account in Account.objects.all().order_by("number"):
        if account.number in mapped_numbers:
            continue
        balance = _net(account, entry__date__lte=as_of)
        if balance:
            key = account.name.casefold()
            names.setdefault(key, account.name)
            zoho.setdefault(key, ZERO)
            ours[key], notes[key] = balance, "Not in Zoho's report"
    return [Diff(names[k], ours[k], zoho[k], notes.get(k, "")) for k in zoho]


def _profit_and_loss(rows, plan, year) -> list[Diff]:
    by_name = {p.name.casefold(): p for p in plan}
    diffs, zoho_net, zoho_has_net = [], ZERO, False
    zoho: dict[str, Decimal] = defaultdict(lambda: ZERO)
    names: dict[str, str] = {}
    for row in rows:
        key = row["account"].casefold()
        if key == "net income":
            zoho_net, zoho_has_net = mapping.money(row["amount"]), True
            continue
        names.setdefault(key, row["account"])
        zoho[key] += mapping.money(row["amount"])
    ours_net = ZERO
    seen = set()
    for key, amount in zoho.items():
        plan_account = by_name.get(key)
        account = Account.objects.get(number=plan_account.number) if plan_account else None
        if account is None or account.type not in (AccountType.INCOME, AccountType.EXPENSE):
            diffs.append(Diff(names[key], ZERO, amount, "Not an income or expense account"))
            continue
        seen.add(account.pk)
        diffs.append(Diff(names[key], _activity(account, year), amount))
    for account in Account.objects.filter(type__in=[AccountType.INCOME, AccountType.EXPENSE]).order_by("number"):
        if account.pk not in seen:
            activity = _activity(account, year)
            if activity:
                diffs.append(Diff(account.name, activity, ZERO, "Not in Zoho's report"))
    for account in Account.objects.filter(type__in=[AccountType.INCOME, AccountType.EXPENSE]):
        activity = _activity(account, year)
        ours_net += activity if account.type == AccountType.INCOME else -activity
    if not zoho_has_net:
        for key, amount in zoho.items():
            plan_account = by_name.get(key)
            account = Account.objects.filter(number=plan_account.number).first() if plan_account else None
            if account is not None and account.type == AccountType.INCOME:
                zoho_net += amount
            elif account is not None and account.type == AccountType.EXPENSE:
                zoho_net -= amount
    diffs.append(Diff("Net income", ours_net, zoho_net))
    return diffs


def _activity(account: Account, year: int) -> Decimal:
    """Income as credit minus debit, expense as debit minus credit: the way a P&L shows them."""
    net = _net(account, entry__date__year=year)
    return -net if account.type == AccountType.INCOME else net


def _total_row(rows) -> Decimal:
    return sum((mapping.money(r.get("total", "")) for r in rows), ZERO)


def _quantities(rows) -> list[Diff]:
    zoho = {r["sku"]: Decimal(r["quantity"].replace(",", "") or "0") for r in rows}
    diffs = []
    for item in Item.objects.filter(item_type=ItemType.STOCK).order_by("sku"):
        ours = Decimal(on_hand(item))
        if ours or item.sku in zoho:
            diffs.append(Diff(item.sku, ours, zoho.pop(item.sku, ZERO)))
    diffs.extend(Diff(sku, ZERO, qty, "No such item") for sku, qty in zoho.items())
    return diffs


def _ever_negative(after, warnings) -> set[str]:
    skus = {item.sku for item, _ in after.negative_items}
    for warning in warnings:
        match = NEGATIVE_WARNING.match(warning)
        if match:
            skus.add(match.group(1))
    return skus


def _inventory_value(rows, after, warnings) -> list[Diff]:
    if rows is None:
        return []
    zoho = {r["sku"]: mapping.money(r["value"]) for r in rows}
    negative = _ever_negative(after, warnings)
    diffs = []
    for item in Item.objects.filter(item_type=ItemType.STOCK).order_by("sku"):
        if item.sku in zoho or on_hand(item):
            diffs.append(Diff(item.sku, stock_value(item), zoho.pop(item.sku, ZERO),
                              "ever negative" if item.sku in negative else ""))
    diffs.extend(Diff(sku, ZERO, value, "No such item") for sku, value in zoho.items())
    return diffs


# ---- document counts and totals ----
def _export_docs(export, short, id_column, total_of) -> tuple[int, Decimal]:
    seen, total = set(), ZERO
    for row in export.files.get(short, []):
        key = row[id_column]
        if key not in seen:
            seen.add(key)
            total += total_of(row)
    return len(seen), total


def _ours(kind, total_of) -> tuple[int, Decimal]:
    ids, total = set(), ZERO
    for record in ZohoRecord.objects.filter(kind=kind):
        ids.add(record.zoho_id.split(":")[0])
        if record.target is not None:
            total += total_of(record.target)
    return len(ids), total


def _counts(export) -> list[Diff]:
    money = mapping.money
    types = (
        ("Invoices", "invoice", "invoices", "Invoice ID", invoice_total, lambda r: money(r["Total"])),
        ("Bills", "bill", "bills", "Bill ID", bill_total, lambda r: money(r["Total"])),
        ("Payments", "customer_payment", "customer_payments", "CustomerPayment ID",
         lambda o: o.amount, lambda r: money(r["Amount"])),
        ("Vendor payments", "vendor_payment", "vendor_payments", "VendorPayment ID",
         lambda o: o.amount, lambda r: money(r["Amount"])),
        ("Credit notes", "credit_note", "credit_notes", "CreditNotes ID", credit_total, lambda r: money(r["Total"])),
        ("Vendor credits", "vendor_credit", "vendor_credits", "VendorCredit ID",
         vendor_credit_total, lambda r: money(r["Total"])),
    )
    diffs = []
    for label, kind, short, id_column, ours_total, zoho_total in types:
        count, total = _ours(kind, ours_total)
        zoho_count, zoho_sum = _export_docs(export, short, id_column, zoho_total)
        diffs.append(Diff(f"{label}: count", Decimal(count), Decimal(zoho_count)))
        diffs.append(Diff(f"{label}: total", total, zoho_sum))
    return diffs


def compare(reports_dir: Path, as_of: date, after, plan, warnings, export) -> TieOut:
    """Reports folder (all files optional, a missing one reads "Not provided" and fails its check):
    trial_balance.csv (account,debit,credit), pl_YYYY.csv (account,amount; a row named "Net income" is Zoho's net,
    otherwise it is worked out from the rows), ar_aging.csv / ap_aging.csv (total), inventory_summary.csv
    (sku,quantity), inventory_valuation.csv (sku,value). Trial balance is as of `as_of`; stock is as it stands now."""
    folder = Path(reports_dir)
    checks = []
    rows = _read(folder / "trial_balance.csv")
    checks.append(_check("Trial balance", None if rows is None else _trial_balance(rows, plan, as_of)))
    pl_files = sorted(folder.glob("pl_*.csv")) if folder.is_dir() else []
    if pl_files:
        for path in pl_files:
            year = path.stem[3:]
            diffs = _profit_and_loss(_read(path), plan, int(year)) if year.isdigit() else None
            checks.append(_check(f"Profit and loss {year}", diffs))
    else:
        checks.append(_check("Profit and loss", None))
    rows = _read(folder / "ar_aging.csv")
    checks.append(_check("A/R aging", None if rows is None else [Diff("Total", ar_aging(as_of).totals.total, _total_row(rows))]))
    rows = _read(folder / "ap_aging.csv")
    checks.append(_check("A/P aging", None if rows is None else [Diff("Total", ap_aging(as_of).totals.total, _total_row(rows))]))
    rows = _read(folder / "inventory_summary.csv")
    checks.append(_check("Inventory quantities", None if rows is None else _quantities(rows)))
    value_rows = _read(folder / "inventory_valuation.csv")
    return TieOut(
        checks=checks, inventory_value=_inventory_value(value_rows, after, warnings), counts=_counts(export),
        after=after, account_map=list(plan), warnings=list(warnings), as_of=as_of,
        inventory_value_provided=value_rows is not None)


# ---- rendering ----
def _context(t: TieOut, pdf: bool) -> dict:
    return {
        "t": t, "pdf": pdf, "css": CSS_PATH.read_text(), "font_uri": FONT_DIR.as_uri(),
        "negative_skus": _ever_negative(t.after, t.warnings),
    }


def render_html(t: TieOut, pdf: bool = False) -> str:
    return render_to_string("migration/tieout.html", _context(t, pdf))


def render_pdf(t: TieOut) -> bytes:
    html = HTML(string=render_html(t, pdf=True), base_url=Path(settings.BASE_DIR).as_uri() + "/")
    return html.write_pdf()

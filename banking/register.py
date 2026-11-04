"""The bank register: every posted line on one bank or card account, newest first, with a running balance.

The running balance is a SQL window over ALL of the account's in-ledger lines; the rows a page shows are picked
from it afterwards, so date, type, and text filters never change a row's balance.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import connection
from django.db.models import Exists, F, OuterRef, Q, Sum, Window
from django.db.models.functions import Coalesce
from django.urls import reverse

from ledger.balances import money
from ledger.models import JournalLine
from ledger.sources import _LABELS, describe

from .models import BankAccount, Match, ReconciliationItem, ReconStatus, TxnStatus

ZERO = Decimal("0.00")
MANUAL = "manual"
TYPE_CHOICES = [("", "All types"), (MANUAL, "Manual entry"), ("migration", "Zoho import"),
                *((st, label) for st, (label, _) in _LABELS.items())]
RECONCILED_CHOICES = [("", "Any"), ("yes", "Reconciled"), ("no", "Not reconciled")]

RECONCILED = "Reconciled"
MATCHED = "Matched"
FROM_BANK = "From bank"
MANUALLY_ADDED = "Manually added"


@dataclass
class RegisterFilters:
    start: date | None = None
    end: date | None = None
    type: str = ""
    q: str = ""
    reconciled: str = ""


@dataclass
class RegisterRow:
    line: JournalLine
    date: date
    reference: str
    type_label: str
    counterparty: str
    status: str
    deposit: Decimal | None
    withdrawal: Decimal | None
    balance: Decimal
    url: str | None


def _sign(account: BankAccount) -> int:
    # A card or loan shows what is owed, so a charge (credit) raises the shown balance.
    return -1 if account.is_liability else 1


def ledger_balance(account: BankAccount) -> Decimal:
    """"Amount in books": the account's balance from every posted line, as the Bank accounts list shows it."""
    t = JournalLine.objects.in_ledger().filter(account_id=account.ledger_account_id).aggregate(
        d=Coalesce(Sum("debit"), ZERO), c=Coalesce(Sum("credit"), ZERO))
    return money((money(t["d"]) - money(t["c"])) * _sign(account))


def _reconciled():
    return Exists(ReconciliationItem.objects.filter(
        journal_line=OuterRef("pk"), cleared=True, reconciliation__status=ReconStatus.FINISHED))


def filtered_lines(account: BankAccount, f: RegisterFilters):
    lines = (JournalLine.objects.in_ledger().filter(account_id=account.ledger_account_id)
             .annotate(is_reconciled=_reconciled()).select_related("entry"))
    if f.start:
        lines = lines.filter(entry__date__gte=f.start)
    if f.end:
        lines = lines.filter(entry__date__lte=f.end)
    if f.type == MANUAL:
        lines = lines.filter(entry__source_type="")
    elif f.type == "migration":
        lines = lines.filter(entry__source_type__startswith="migration.")
    elif f.type:
        lines = lines.filter(entry__source_type=f.type)
    q = f.q.strip()
    if q:
        text = (Q(entry__memo__icontains=q) | Q(entry__reference__icontains=q) | Q(entry__source_id__icontains=q)
                | Q(description__icontains=q))
        try:
            amount = Decimal(q.replace(",", "").lstrip("$"))
            text |= Q(debit=amount) | Q(credit=amount)
        except InvalidOperation:
            pass
        lines = lines.filter(text)
    if f.reconciled == "yes":
        lines = lines.filter(is_reconciled=True)
    elif f.reconciled == "no":
        lines = lines.filter(is_reconciled=False)
    return lines.order_by("-entry__date", "-entry_id", "-id")


def running_balances(account: BankAccount, line_ids: list[int]) -> dict[int, Decimal]:
    """Each line's running balance (signed as the account shows it), from one windowed query."""
    if not line_ids:
        return {}
    inner = (JournalLine.objects.in_ledger().filter(account_id=account.ledger_account_id)
             .annotate(running=Window(Sum(F("debit") - F("credit")),
                                      order_by=[F("entry__date").asc(), F("entry_id").asc(), F("id").asc()]))
             .values("id", "running"))
    sql, params = inner.query.sql_with_params()
    marks = ", ".join(["%s"] * len(line_ids))
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT reg.id, reg.running FROM ({sql}) reg WHERE reg.id IN ({marks})",
                       [*params, *line_ids])
        rows = cursor.fetchall()
    sign = _sign(account)
    # SQLite sums decimals as floats; money() brings each back to exact cents.
    return {pk: money(Decimal(str(value)) * sign) for pk, value in rows}


def _counterparties(entries) -> dict[int, str]:
    """"Customer: name" or "Vendor: name" per entry, one query per document type."""
    from purchasing.models import Bill, VendorCredit, VendorPayment
    from sales.models import CreditNote, Invoice, Payment, Refund

    models = {
        "sales.payment": (Payment, "customer", "Customer"), "sales.invoice": (Invoice, "customer", "Customer"),
        "sales.credit_note": (CreditNote, "customer", "Customer"), "sales.refund": (Refund, "customer", "Customer"),
        "purchasing.payment": (VendorPayment, "vendor", "Vendor"), "purchasing.bill": (Bill, "vendor", "Vendor"),
        "purchasing.vendor_credit": (VendorCredit, "vendor", "Vendor"),
    }
    wanted: dict[str, set[str]] = {}
    for e in entries:
        if e.source_type in models and e.source_id:
            wanted.setdefault(e.source_type, set()).add(e.source_id)
    names: dict[tuple[str, str], str] = {}
    for source_type, numbers in wanted.items():
        model, party, label = models[source_type]
        for number, name in model.objects.filter(number__in=numbers).values_list("number", f"{party}__name"):
            names[(source_type, number)] = f"{label}: {name}"
    return {e.pk: names.get((e.source_type, e.source_id), "") for e in entries}


def _bank_statuses(entry_ids: set[int]) -> dict[int, str]:
    """Entry id -> the status of the bank row matched to it (directly, or through its document)."""
    links = {
        "journal_entry_id": "journal_entry_id", "sales_payment__journal_entry_id": "sales_payment__journal_entry_id",
        "deposit__journal_entry_id": "deposit__journal_entry_id", "refund__journal_entry_id": "refund__journal_entry_id",
        "vendor_payment__journal_entry_id": "vendor_payment__journal_entry_id",
    }
    q = Q()
    for field in links:
        q |= Q(**{f"{field}__in": entry_ids})
    out: dict[int, str] = {}
    for row in Match.objects.filter(q).values("bank_transaction__status", *links):
        for field in links:
            if row[field] in entry_ids:
                out[row[field]] = row["bank_transaction__status"]
    return out


def _type_label(entry) -> str:
    source = describe(entry)
    return source["label"] if source else "Manual entry"


def build_rows(account: BankAccount, lines: list[JournalLine]) -> list[RegisterRow]:
    balances = running_balances(account, [l.pk for l in lines])
    entries = {l.entry_id: l.entry for l in lines}
    parties = _counterparties(entries.values())
    bank = _bank_statuses(set(entries))
    sign = _sign(account)
    rows = []
    for line in lines:
        e = line.entry
        if getattr(line, "is_reconciled", False):
            status = RECONCILED
        elif e.pk in bank:
            status = FROM_BANK if bank[e.pk] == TxnStatus.BOOKED else MATCHED
        else:
            status = MANUALLY_ADDED
        change = (line.debit - line.credit) * sign
        source = describe(e)
        url = (source or {}).get("url") or reverse("books:journal-detail", args=[e.pk])
        rows.append(RegisterRow(
            line=line, date=e.date, reference=e.reference or e.source_id, type_label=_type_label(e),
            counterparty=parties.get(e.pk) or e.memo, status=status,
            deposit=money(change) if change > 0 else None, withdrawal=money(-change) if change < 0 else None,
            balance=balances[line.pk], url=url))
    return rows

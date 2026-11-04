"""Cash-basis profit and loss.

Every amount comes from posted ledger lines. Payment links (which payment or credit settled which invoice or bill,
and on what date) only decide WHEN an invoice's income lines and a bill's expense lines count: each settlement
counts its share (settled / document total) of every such line, on the settlement date. A settlement that is
later removed counts back out on its removal date.

Everything else on P&L accounts (cost of goods sold from stock movements, stock adjustments, manual journals,
bank-only expenses, imported journals) counts as posted, the way most small-business cash reports treat non-cash
costs. Prepayments not yet applied to an invoice are not income yet.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import Q, Sum

from .balances import money
from .models import AccountType, JournalLine
from .reports import ProfitAndLoss, profit_and_loss_from_totals

ZERO = Decimal("0.00")
P_AND_L = (AccountType.INCOME, AccountType.EXPENSE)

# source_type -> (account type whose lines wait for settlement, settles via "ar" or "ap", sign of the doc total)
ALLOCATED = {
    "sales.invoice": (AccountType.INCOME, "ar", 1),       # A/R debit
    "sales.credit_note": (AccountType.INCOME, "ar", -1),  # A/R credit
    "purchasing.bill": (AccountType.EXPENSE, "ap", -1),   # A/P credit
    "purchasing.vendor_credit": (AccountType.EXPENSE, "ap", 1),  # A/P debit
}


def _control_accounts() -> dict[str, int | None]:
    from purchasing.models import PurchasingSettings
    from sales.models import SalesSettings

    return {"ar": SalesSettings.load().ar_account_id, "ap": PurchasingSettings.load().ap_account_id}


def _events(start: date, end: date) -> dict[tuple[str, str], Decimal]:
    """(source_type, document number) -> amount settled within [start, end], net of removals in the range."""
    from purchasing.models import BillPaymentApplication, VendorCreditApplication
    from sales.models import CreditApplication, PaymentApplication, Refund

    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)

    def add(key, amount, applied_on, removed, removed_on):
        if start <= applied_on <= end:
            out[key] += amount
        if removed and removed_on is not None and start <= removed_on <= end:
            out[key] -= amount

    fields = ("amount", "applied_on", "removed", "removed_on")
    for a in PaymentApplication.objects.select_related("invoice").only("invoice__number", *fields):
        add(("sales.invoice", a.invoice.number), a.amount, a.applied_on, a.removed, a.removed_on)
    for a in CreditApplication.objects.select_related("invoice", "credit_note").only(
            "invoice__number", "credit_note__number", *fields):
        add(("sales.invoice", a.invoice.number), a.amount, a.applied_on, a.removed, a.removed_on)
        add(("sales.credit_note", a.credit_note.number), a.amount, a.applied_on, a.removed, a.removed_on)
    for r in Refund.objects.filter(source_credit_note__isnull=False).select_related("source_credit_note"):
        voided = r.status == "VOID"
        add(("sales.credit_note", r.source_credit_note.number), r.amount, r.date, voided, r.voided_on)
    for a in BillPaymentApplication.objects.select_related("bill").only("bill__number", *fields):
        add(("purchasing.bill", a.bill.number), a.amount, a.applied_on, a.removed, a.removed_on)
    for a in VendorCreditApplication.objects.select_related("bill", "credit").only(
            "bill__number", "credit__number", *fields):
        add(("purchasing.bill", a.bill.number), a.amount, a.applied_on, a.removed, a.removed_on)
        add(("purchasing.vendor_credit", a.credit.number), a.amount, a.applied_on, a.removed, a.removed_on)
    return out


def _allocated_q() -> Q:
    q = Q()
    for source, (account_type, _, _) in ALLOCATED.items():
        q |= Q(entry__source_type=source, account__type=account_type)
    return q


def profit_and_loss_cash(start: date, end: date) -> ProfitAndLoss:
    ledger = JournalLine.objects.in_ledger()
    totals: dict[int, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])

    # 1. P&L lines that don't wait for a payment, as posted in the range.
    as_posted = (ledger.filter(entry__date__gte=start, entry__date__lte=end, account__type__in=P_AND_L)
                 .exclude(_allocated_q()).values("account_id").annotate(d=Sum("debit"), c=Sum("credit")))
    for r in as_posted:
        totals[r["account_id"]][0] += money(r["d"])
        totals[r["account_id"]][1] += money(r["c"])

    # 2. Invoice, credit note, bill, and vendor credit lines, by the share settled in the range.
    events = _events(start, end)
    if events:
        control = _control_accounts()
        sources = list(ALLOCATED)
        numbers = {n for (_, n) in events}
        rows = (ledger.filter(entry__source_type__in=sources, entry__source_id__in=numbers)
                .values("entry__source_type", "entry__source_id", "account_id", "account__type")
                .annotate(d=Sum("debit"), c=Sum("credit")))
        doc_total: dict[tuple, Decimal] = defaultdict(lambda: ZERO)
        doc_lines: dict[tuple, list] = defaultdict(list)
        for r in rows:
            key = (r["entry__source_type"], r["entry__source_id"])
            account_type, side, sign = ALLOCATED[key[0]]
            debit, credit = money(r["d"]), money(r["c"])
            if r["account_id"] == control[side]:
                doc_total[key] += sign * (debit - credit)
            elif r["account__type"] == account_type:
                doc_lines[key].append((r["account_id"], debit - credit))
        for key, settled in events.items():
            total = doc_total.get(key, ZERO)
            if total <= 0 or settled == 0:
                continue
            share = min(settled / total, Decimal("1"))  # an over-settled document can't count more than once
            for account_id, net in doc_lines.get(key, []):
                amount = money(net * share)
                if amount > 0:
                    totals[account_id][0] += amount
                elif amount < 0:
                    totals[account_id][1] += -amount

    return profit_and_loss_from_totals(start, end, {k: (money(d), money(c)) for k, (d, c) in totals.items()})

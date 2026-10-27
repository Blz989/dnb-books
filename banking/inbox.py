"""The bank feed inbox: suggest, then a person confirms. Nothing here books an existing document twice."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from ledger.models import Account, JournalEntry
from ledger.posting import LineSpec, post_entry
from purchasing.models import VendorPayment, VendorPaymentMethod
from sales.models import Deposit, DocStatus, Payment, PaymentMethod, Refund

from .checknumbers import normalize
from .models import BankTransaction, Match, Rule, TxnStatus

ZERO = Decimal("0.00")
DOCUMENT_WINDOW_DAYS = 10
DEPOSIT_WINDOW_DAYS = 5
ALREADY_MATCHED = "That item is already matched to another bank transaction."


class InboxError(Exception):
    """A problem a person can fix. Messages are plain language."""


@dataclass(frozen=True)
class Candidate:
    kind: str  # vendor_check, document, deposit, transfer, rule
    target: object
    score: int
    label: str


def _days(txn: BankTransaction, day) -> int:
    return abs((txn.date - day).days)


def _number(target) -> str:
    return getattr(target, "number", None) or f"{getattr(target, 'pk', 0):012d}"


def _is_matched(target) -> bool:
    if isinstance(target, Payment):
        return Match.objects.filter(sales_payment=target).exists()
    if isinstance(target, Deposit):
        return Match.objects.filter(deposit=target).exists()
    if isinstance(target, Refund):
        return Match.objects.filter(refund=target).exists()
    if isinstance(target, VendorPayment):
        return Match.objects.filter(vendor_payment=target).exists()
    return False


def _window(txn: BankTransaction, days: int) -> dict:
    return {"date__gte": txn.date - timedelta(days=days), "date__lte": txn.date + timedelta(days=days)}


def _document_candidates(txn: BankTransaction) -> list[Candidate]:
    ledger = txn.bank_account.ledger_account
    found: list[Candidate] = []
    window = _window(txn, DOCUMENT_WINDOW_DAYS)

    for p in Payment.objects.filter(
        status=DocStatus.OPEN, deposited_to=ledger, method__in=(PaymentMethod.ACH, PaymentMethod.WIRE),
        amount=txn.amount, bank_match__isnull=True, **window,
    ).select_related("customer"):
        found.append(Candidate("document", p, 80 - _days(txn, p.date), f"Payment {p.number} from {p.customer.name}"))

    for r in Refund.objects.filter(
        status=DocStatus.OPEN, bank_account=ledger, amount=-txn.amount, bank_match__isnull=True, **window,
    ).select_related("customer"):
        found.append(Candidate("document", r, 80 - _days(txn, r.date), f"Refund {r.number} to {r.customer.name}"))

    wanted_check = normalize(txn.check_number)
    for v in VendorPayment.objects.filter(
        status=DocStatus.OPEN, paid_from=ledger, amount=-txn.amount, bank_match__isnull=True,
    ).select_related("vendor"):
        if wanted_check and v.method == VendorPaymentMethod.CHECK and normalize(v.check_number) == wanted_check:
            found.append(Candidate(
                "vendor_check", v, 100, f"Check {v.check_number} to {v.vendor.name} ({v.number})"))
        elif abs((txn.date - v.date).days) <= DOCUMENT_WINDOW_DAYS:
            what = f"Check {v.check_number} to" if v.check_number else "Payment to"
            found.append(Candidate("document", v, 80 - _days(txn, v.date), f"{what} {v.vendor.name} ({v.number})"))
    return found


def _deposit_candidates(txn: BankTransaction) -> list[Candidate]:
    if txn.amount <= 0:
        return []
    ledger = txn.bank_account.ledger_account
    found = []
    deposits = Deposit.objects.filter(
        status=DocStatus.OPEN, bank_account=ledger, bank_match__isnull=True, **_window(txn, DEPOSIT_WINDOW_DAYS))
    for dep in deposits.select_related("journal_entry"):
        total = dep.journal_entry.lines.filter(account=ledger).aggregate(t=Sum("debit"))["t"] or ZERO
        if total == txn.amount:
            found.append(Candidate("deposit", dep, 85 - _days(txn, dep.date), f"Deposit {dep.number}"))
    return found


def _rule_candidate(txn: BankTransaction) -> list[Candidate]:
    for rule in Rule.objects.filter(is_active=True).select_related("account").order_by("position", "id"):
        if rule.bank_account_id not in (None, txn.bank_account_id):
            continue
        if rule.matches(txn.description):
            return [Candidate("rule", rule, 50, f"Book to {rule.account.number} {rule.account.name}")]
    return []


def candidates(txn: BankTransaction) -> list[Candidate]:
    found = _document_candidates(txn) + _deposit_candidates(txn) + _transfer_candidates(txn) + _rule_candidate(txn)
    return sorted(found, key=lambda c: (-c.score, _days(txn, getattr(c.target, "date", txn.date)), _number(c.target)))


def _transfer_candidates(txn: BankTransaction) -> list[Candidate]:
    return []  # transfers are added in the transfers task


def suggest(txn: BankTransaction) -> Candidate | None:
    found = candidates(txn)
    return found[0] if found else None


def _match_kwargs(target) -> dict:
    if isinstance(target, Payment):
        return {"sales_payment": target}
    if isinstance(target, Deposit):
        return {"deposit": target}
    if isinstance(target, Refund):
        return {"refund": target}
    if isinstance(target, VendorPayment):
        return {"vendor_payment": target}
    if isinstance(target, JournalEntry):
        return {"journal_entry": target}
    raise InboxError("That can't be matched to a bank transaction.")


def _lock(txn: BankTransaction) -> BankTransaction:
    current = BankTransaction.objects.select_related("bank_account__ledger_account").get(pk=txn.pk)
    if current.status != TxnStatus.NEW:
        raise InboxError("This bank transaction was already handled.")
    return current


def link(txn: BankTransaction, target, *, user, status: str = TxnStatus.MATCHED) -> Match:
    """Record that `txn` is the bank's view of `target`. Posts nothing."""
    with transaction.atomic():
        current = _lock(txn)
        if _is_matched(target):
            raise InboxError(ALREADY_MATCHED)
        match = Match.objects.create(bank_transaction=current, confirmed_by=user, **_match_kwargs(target))
        current.status = status
        current.save()
    txn.status = status
    return match


def new_entry(txn: BankTransaction, *, account: Account, user, memo: str = "") -> Match:
    """Book the bank row against `account`. A positive amount is always a debit to the bank-side account."""
    with transaction.atomic():
        current = _lock(txn)
        bank_side = current.bank_account.ledger_account
        amount = abs(current.amount)
        if current.amount >= 0:
            lines = [LineSpec(bank_side, debit=amount), LineSpec(account, credit=amount)]
        else:
            lines = [LineSpec(account, debit=amount), LineSpec(bank_side, credit=amount)]
        entry = post_entry(
            date=current.date, user=user, memo=memo or current.description, reference=current.check_number,
            source_type="banking.entry", source_id=f"BT-{current.pk}", lines=lines,
        )
        match = Match.objects.create(bank_transaction=current, journal_entry=entry, confirmed_by=user)
        current.status = TxnStatus.BOOKED
        current.save()
    txn.status = TxnStatus.BOOKED
    return match


def confirm(txn: BankTransaction, candidate: Candidate, *, user) -> Match:
    if candidate.kind == "rule":
        rule = candidate.target
        return new_entry(txn, account=rule.account, user=user, memo=rule.memo)
    if candidate.kind == "transfer":
        return _confirm_transfer(txn, candidate, user=user)
    return link(txn, candidate.target, user=user)


def _confirm_transfer(txn: BankTransaction, candidate: Candidate, *, user) -> Match:
    raise InboxError("That can't be matched to a bank transaction.")


def exclude(txn: BankTransaction, *, reason: str, user) -> None:
    reason = (reason or "").strip()
    if not reason:
        raise InboxError("Enter a reason.")
    with transaction.atomic():
        current = _lock(txn)
        current.status, current.exclude_reason = TxnStatus.EXCLUDED, reason
        current.save()
    txn.status, txn.exclude_reason = TxnStatus.EXCLUDED, reason


def restore(txn: BankTransaction, *, user) -> None:
    with transaction.atomic():
        current = BankTransaction.objects.get(pk=txn.pk)
        if current.status != TxnStatus.EXCLUDED:
            raise InboxError("Only an excluded transaction can be restored.")
        current.status, current.exclude_reason = TxnStatus.NEW, ""
        current.save()
    txn.status, txn.exclude_reason = TxnStatus.NEW, ""


def bulk_confirm(txns: Sequence[BankTransaction], *, user) -> int:
    done = 0
    for txn in txns:
        if txn.status != TxnStatus.NEW:
            continue
        candidate = suggest(txn)
        if candidate is None:
            continue
        try:
            confirm(txn, candidate, user=user)
        except InboxError:
            continue  # taken by an earlier row in this batch
        done += 1
    return done


def release_matches(entry: JournalEntry) -> None:
    """Void hook: a bank row matched to a document that was just voided goes back to NEW."""
    from django.db.models import Q

    matches = Match.objects.filter(
        Q(journal_entry=entry) | Q(sales_payment__journal_entry=entry) | Q(deposit__journal_entry=entry)
        | Q(refund__journal_entry=entry) | Q(vendor_payment__journal_entry=entry)
    ).select_related("bank_transaction")
    for match in matches:
        txn = match.bank_transaction
        match.delete()
        txn.status = TxnStatus.NEW
        txn.save()

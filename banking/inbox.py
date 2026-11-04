"""The bank feed inbox: suggest, then a person confirms. Nothing here books an existing document twice."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from contacts.models import Customer, Vendor
from ledger.models import Account, JournalEntry, JournalLine
from ledger.posting import LineSpec, post_entry
from purchasing.models import VendorPayment, VendorPaymentMethod
from sales.models import Deposit, DocStatus, Payment, PaymentMethod, Refund

from .checknumbers import normalize
from .models import AccountKind, BankTransaction, Match, Rule, Transfer, TransferStatus, TxnStatus

ZERO = Decimal("0.00")
DOCUMENT_WINDOW_DAYS = 10
DEPOSIT_WINDOW_DAYS = 5
TRANSFER_WINDOW_DAYS = 5
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


def _is_matched(target, txn: BankTransaction | None = None) -> bool:
    if isinstance(target, JournalEntry):
        # A transfer's entry is matched once per bank account, so each side can link to it.
        rows = Match.objects.filter(journal_entry=target)
        return (rows.filter(bank_transaction__bank_account=txn.bank_account) if txn else rows).exists()
    if isinstance(target, Payment):
        return Match.objects.filter(sales_payment=target).exists()
    if isinstance(target, Deposit):
        return Match.objects.filter(deposit=target).exists()
    if isinstance(target, Refund):
        return Match.objects.filter(refund=target).exists()
    if isinstance(target, VendorPayment):
        return Match.objects.filter(vendor_payment=target).exists()
    return False


def _within(txn: BankTransaction, day, days: int) -> bool:
    return txn.date - timedelta(days=days) <= day <= txn.date + timedelta(days=days)


class CandidatePool:
    """Everything the suggestion rules can match for a set of bank rows, read in a fixed number of queries.

    The inbox builds one pool per page; a single row gets a pool of its own. Each rule below filters the pool in
    Python with the same conditions it once sent to the database, in the same order (each model's default ordering).
    """

    def __init__(self, txns: Sequence[BankTransaction]):
        txns = list(txns)
        self.rules = list(Rule.objects.filter(is_active=True).select_related("account").order_by("position", "id"))
        if not txns:
            self.payments = self.refunds = self.vendor_payments = self.deposits = self.transfers = []
            self.deposit_totals, self.matched_transfers = {}, set()
            return
        ledgers = {t.bank_account.ledger_account_id for t in txns}
        accounts = {t.bank_account_id for t in txns}
        amounts = {t.amount for t in txns}
        negated = {-a for a in amounts}
        first, last = min(t.date for t in txns), max(t.date for t in txns)
        doc_window = {"date__gte": first - timedelta(days=DOCUMENT_WINDOW_DAYS),
                      "date__lte": last + timedelta(days=DOCUMENT_WINDOW_DAYS)}
        deposit_window = {"date__gte": first - timedelta(days=DEPOSIT_WINDOW_DAYS),
                          "date__lte": last + timedelta(days=DEPOSIT_WINDOW_DAYS)}
        transfer_window = {"date__gte": first - timedelta(days=TRANSFER_WINDOW_DAYS),
                           "date__lte": last + timedelta(days=TRANSFER_WINDOW_DAYS)}

        self.payments = list(Payment.objects.filter(
            status=DocStatus.OPEN, deposited_to_id__in=ledgers, method__in=(PaymentMethod.ACH, PaymentMethod.WIRE),
            amount__in=amounts, bank_match__isnull=True, **doc_window,
        ).select_related("customer"))
        self.refunds = list(Refund.objects.filter(
            status=DocStatus.OPEN, bank_account_id__in=ledgers, amount__in=negated, bank_match__isnull=True, **doc_window,
        ).select_related("customer"))
        self.vendor_payments = list(VendorPayment.objects.filter(
            status=DocStatus.OPEN, paid_from_id__in=ledgers, amount__in=negated, bank_match__isnull=True,
        ).select_related("vendor"))

        if any(t.amount > 0 for t in txns):
            self.deposits = list(Deposit.objects.filter(
                status=DocStatus.OPEN, bank_account_id__in=ledgers, bank_match__isnull=True, **deposit_window,
            ).select_related("journal_entry"))
        else:
            self.deposits = []
        self.deposit_totals = {
            (r["entry_id"], r["account_id"]): r["t"] or ZERO
            for r in JournalLine.objects.filter(entry_id__in=[d.journal_entry_id for d in self.deposits],
                                                account_id__in=ledgers)
            .values("entry_id", "account_id").annotate(t=Sum("debit"))
        } if self.deposits else {}

        self.transfers = list(Transfer.objects.filter(
            Q(from_account_id__in=accounts) | Q(to_account_id__in=accounts),
            status=TransferStatus.POSTED, amount__in={abs(a) for a in amounts}, **transfer_window,
        ).select_related("from_account__ledger_account", "to_account__ledger_account"))
        self.matched_transfers = set(Match.objects.filter(
            journal_entry_id__in=[t.journal_entry_id for t in self.transfers], bank_transaction__bank_account_id__in=accounts,
        ).values_list("journal_entry_id", "bank_transaction__bank_account_id")) if self.transfers else set()


def _document_candidates(txn: BankTransaction, pool: CandidatePool) -> list[Candidate]:
    ledger_id = txn.bank_account.ledger_account_id
    found: list[Candidate] = []

    for p in pool.payments:
        if p.deposited_to_id == ledger_id and p.amount == txn.amount and _within(txn, p.date, DOCUMENT_WINDOW_DAYS):
            found.append(Candidate("document", p, 80 - _days(txn, p.date), f"Payment {p.number} from {p.customer.name}"))

    for r in pool.refunds:
        if r.bank_account_id == ledger_id and r.amount == -txn.amount and _within(txn, r.date, DOCUMENT_WINDOW_DAYS):
            found.append(Candidate("document", r, 80 - _days(txn, r.date), f"Refund {r.number} to {r.customer.name}"))

    wanted_check = normalize(txn.check_number)
    for v in pool.vendor_payments:
        if v.paid_from_id != ledger_id or v.amount != -txn.amount:
            continue
        if wanted_check and v.method == VendorPaymentMethod.CHECK and normalize(v.check_number) == wanted_check:
            found.append(Candidate(
                "vendor_check", v, 100, f"Check {v.check_number} to {v.vendor.name} ({v.number})"))
        elif abs((txn.date - v.date).days) <= DOCUMENT_WINDOW_DAYS:
            what = f"Check {v.check_number} to" if v.check_number else "Payment to"
            found.append(Candidate("document", v, 80 - _days(txn, v.date), f"{what} {v.vendor.name} ({v.number})"))
    return found


def _deposit_candidates(txn: BankTransaction, pool: CandidatePool) -> list[Candidate]:
    if txn.amount <= 0:
        return []
    ledger_id = txn.bank_account.ledger_account_id
    found = []
    for dep in pool.deposits:
        if dep.bank_account_id != ledger_id or not _within(txn, dep.date, DEPOSIT_WINDOW_DAYS):
            continue
        if pool.deposit_totals.get((dep.journal_entry_id, ledger_id), ZERO) == txn.amount:
            found.append(Candidate("deposit", dep, 85 - _days(txn, dep.date), f"Deposit {dep.number}"))
    return found


def _rule_candidate(txn: BankTransaction, pool: CandidatePool) -> list[Candidate]:
    for rule in pool.rules:
        if rule.bank_account_id not in (None, txn.bank_account_id):
            continue
        if rule.matches(txn.description):
            return [Candidate("rule", rule, 50, f"Book to {rule.account.number} {rule.account.name}")]
    return []


def candidates(txn: BankTransaction, pool: CandidatePool | None = None) -> list[Candidate]:
    pool = pool or CandidatePool([txn])
    found = (_document_candidates(txn, pool) + _deposit_candidates(txn, pool) + _transfer_candidates(txn, pool)
             + _rule_candidate(txn, pool))
    return sorted(found, key=lambda c: (-c.score, _days(txn, getattr(c.target, "date", txn.date)), _number(c.target)))


def _transfer_candidates(txn: BankTransaction, pool: CandidatePool) -> list[Candidate]:
    """A transfer posted from the other account's row, seen from this account: same amount, right direction."""
    account_id = txn.bank_account_id
    found = []
    for t in pool.transfers:
        side = t.from_account_id if txn.amount < 0 else t.to_account_id
        if side != account_id or t.amount != abs(txn.amount) or not _within(txn, t.date, TRANSFER_WINDOW_DAYS):
            continue
        if (t.journal_entry_id, account_id) in pool.matched_transfers:
            continue
        found.append(Candidate(
            "transfer", t, 75 - _days(txn, t.date), f"Transfer {t.from_account} to {t.to_account} on {t.date:%b} {t.date.day}"))
    return found


def suggest(txn: BankTransaction, pool: CandidatePool | None = None) -> Candidate | None:
    found = candidates(txn, pool)
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
        if _is_matched(target, current):
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
    return link(txn, candidate.target.journal_entry, user=user)


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


def candidate_key(candidate: Candidate) -> str:
    """A stable id for a suggestion, so a posted choice can be matched against the current list."""
    return f"{candidate.kind}:{candidate.target._meta.label_lower}:{candidate.target.pk}"


def match_label(match: Match) -> str:
    """What a matched or booked bank row was linked to, for the inbox."""
    if match.sales_payment_id:
        return f"Payment {match.sales_payment.number}"
    if match.deposit_id:
        return f"Deposit {match.deposit.number}"
    if match.refund_id:
        return f"Refund {match.refund.number}"
    if match.vendor_payment_id:
        return f"Payment {match.vendor_payment.number}"
    entry = match.journal_entry
    return entry.source_id or entry.display_number


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _clean_amounts(amounts: dict[int, Decimal]) -> dict[int, Decimal]:
    """Blank (zero) entries are dropped; a negative one is an error."""
    cleaned = {}
    for pk, value in amounts.items():
        value = Decimal(value)
        if value < 0:
            raise InboxError("Amounts must be above zero.")
        if value > 0:
            cleaned[pk] = value
    if not cleaned:
        raise InboxError("Enter an amount on at least one line.")
    return cleaned


def fill_oldest_first(open_docs: list[tuple[int, Decimal]], total: Decimal) -> dict[int, Decimal]:
    """Spread `total` over documents given oldest first as (pk, balance); the last one used may be partial."""
    filled: dict[int, Decimal] = {}
    left = Decimal(total)
    for pk, balance in open_docs:
        if left <= 0:
            break
        if balance <= 0:
            continue
        take = min(balance, left)
        filled[pk] = take
        left -= take
    return filled


def pay_bills_from_row(txn: BankTransaction, *, vendor: Vendor, amounts: dict[int, Decimal], user) -> VendorPayment:
    """One vendor payment for the bills chosen, matched to the bank row. All or nothing."""
    from purchasing.errors import PurchasingError
    from purchasing.models import Bill
    from purchasing.posting.payments import pay_vendor

    with transaction.atomic():
        current = _lock(txn)
        if current.amount >= 0:
            raise InboxError("Pay bills is for money going out.")
        amounts = _clean_amounts(amounts)
        total = sum(amounts.values(), ZERO)
        if total != -current.amount:
            raise InboxError(f"The bills you chose add up to {_money(total)}. The bank shows {_money(-current.amount)}.")
        account = current.bank_account
        check_number = (current.check_number or "").strip()
        if check_number:
            method = VendorPaymentMethod.CHECK
        elif account.kind == AccountKind.CARD:
            method = VendorPaymentMethod.CARD
        else:
            method = VendorPaymentMethod.ACH
        bills = {b.pk: b for b in Bill.objects.filter(pk__in=amounts)}
        if len(bills) != len(amounts):
            raise InboxError("One of those bills no longer exists.")
        try:
            payment = pay_vendor(
                vendor=vendor, date=current.date, method=method, amount=total, paid_from=account.ledger_account,
                user=user, check_number=check_number,
                applications=[(bills[pk], amount) for pk, amount in amounts.items()])
        except PurchasingError as error:
            raise InboxError(str(error)) from error
        link(current, payment, user=user)
    txn.status = TxnStatus.MATCHED
    return payment


def receive_payment_from_row(
    txn: BankTransaction, *, customer: Customer, amounts: dict[int, Decimal], method: PaymentMethod, user,
) -> Payment:
    """One customer payment for the invoices chosen. A check goes through Undeposited Funds and a one-payment deposit."""
    from sales.errors import SalesError
    from sales.models import Invoice
    from sales.posting.deposits import make_deposit
    from sales.posting.receipts import receive_payment

    with transaction.atomic():
        current = _lock(txn)
        ledger = current.bank_account.ledger_account
        if current.amount <= 0 or current.bank_account.kind not in (AccountKind.CHECKING, AccountKind.SAVINGS):
            raise InboxError("Receive payment is for money coming in.")
        if method not in (PaymentMethod.ACH, PaymentMethod.WIRE, PaymentMethod.CHECK):
            raise InboxError("Choose check, ACH, or wire.")
        amounts = _clean_amounts(amounts)
        total = sum(amounts.values(), ZERO)
        if total != current.amount:
            raise InboxError(f"The invoices you chose add up to {_money(total)}. The bank shows {_money(current.amount)}.")
        invoices = {i.pk: i for i in Invoice.objects.filter(pk__in=amounts)}
        if len(invoices) != len(amounts):
            raise InboxError("One of those invoices no longer exists.")
        applications = [(invoices[pk], amount) for pk, amount in amounts.items()]
        try:
            if method == PaymentMethod.CHECK:
                payment = receive_payment(
                    customer=customer, date=current.date, method=method, amount=total, user=user,
                    reference=current.check_number, applications=applications)
                deposit = make_deposit(payments=[payment], bank_account=ledger, date=current.date, user=user)
                link(current, deposit, user=user)
            else:
                payment = receive_payment(
                    customer=customer, date=current.date, method=method, amount=total, user=user,
                    reference=current.check_number, bank_account=ledger, applications=applications)
                link(current, payment, user=user)
        except SalesError as error:
            raise InboxError(str(error)) from error
    txn.status = TxnStatus.MATCHED
    return payment

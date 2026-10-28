"""The bank feed inbox: suggest, then a person confirms. Nothing here books an existing document twice."""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from contacts.models import Customer, Vendor
from ledger.models import Account, JournalEntry
from ledger.posting import LineSpec, post_entry
from purchasing.models import VendorPayment, VendorPaymentMethod
from sales.models import Deposit, DocStatus, Payment, PaymentMethod, Refund

from .checknumbers import normalize
from .models import AccountKind, BankTransaction, Match, Rule, Transfer, TransferStatus, TxnStatus

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


TRANSFER_WINDOW_DAYS = 5


def _transfer_candidates(txn: BankTransaction) -> list[Candidate]:
    """A transfer posted from the other account's row, seen from this account: same amount, right direction."""
    account = txn.bank_account
    found = []
    if txn.amount < 0:
        sides = Transfer.objects.filter(from_account=account)
    else:
        sides = Transfer.objects.filter(to_account=account)
    for t in sides.filter(
        status=TransferStatus.POSTED, amount=abs(txn.amount), **_window(txn, TRANSFER_WINDOW_DAYS),
    ).select_related("from_account__ledger_account", "to_account__ledger_account"):
        if Match.objects.filter(journal_entry=t.journal_entry, bank_transaction__bank_account=account).exists():
            continue
        found.append(Candidate(
            "transfer", t, 75 - _days(txn, t.date), f"Transfer {t.from_account} to {t.to_account} on {t.date:%b} {t.date.day}"))
    return found


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

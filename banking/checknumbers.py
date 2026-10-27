"""Check numbers on a bank account: one registry shared by purchasing, sales, and printing.

Imports of purchasing and sales models stay inside functions: those apps call this module, and banking.models
imports them.
"""
from .models import BankAccount, CheckNumber, CheckStatus


class CheckNumberError(Exception):
    """A problem a person can fix. Subclassed by callers so each app's views keep catching their own error."""


def normalize(number: str) -> str:
    """Strip spaces and leading zeros so '002050', ' 2050' and '2050' are the same check."""
    text = "".join((number or "").split()).casefold()
    return text.lstrip("0") or ("0" if text else "")


def _bank_account(ledger_account) -> BankAccount | None:
    return BankAccount.objects.filter(ledger_account=ledger_account).first()


def _legacy_numbers(ledger_account):
    """(normalized number, status word) for checks written before the registry, or on accounts without one."""
    from purchasing.models import VendorPayment, VendorPaymentMethod
    from sales.models import DocStatus, Refund, RefundMethod

    for p in VendorPayment.objects.filter(paid_from=ledger_account, method=VendorPaymentMethod.CHECK).exclude(
            check_number=""):
        yield normalize(p.check_number), "voided" if p.status == DocStatus.VOID else "handwritten"
    for r in Refund.objects.filter(bank_account=ledger_account, method=RefundMethod.CHECK).exclude(reference=""):
        yield normalize(r.reference), "voided" if r.status == DocStatus.VOID else "handwritten"


def assert_free(bank_account_ledger, number: str, error=CheckNumberError) -> None:
    wanted = normalize(number)
    if not wanted:
        return
    status = None
    bank_account = _bank_account(bank_account_ledger)
    if bank_account is not None:
        row = next((c for c in CheckNumber.objects.filter(bank_account=bank_account)
                    if normalize(c.number) == wanted), None)
        if row is not None:
            status = CheckStatus(row.status).label
    if status is None:
        status = next((s for n, s in _legacy_numbers(bank_account_ledger) if n == wanted), None)
    if status is not None:
        raise error(f"Check {number.strip()} was already used on {bank_account_ledger} ({status}).")


def record(bank_account_ledger, number: str, status: CheckStatus, *, vendor_payment=None, refund=None,
           user=None) -> CheckNumber | None:
    """Register a number. Accounts without a bank account record keep working from their documents alone."""
    bank_account = _bank_account(bank_account_ledger)
    if bank_account is None or not normalize(number):
        return None
    return CheckNumber.objects.create(
        bank_account=bank_account, number=normalize(number), status=status, vendor_payment=vendor_payment,
        refund=refund, printed_by=user)


def mark_voided(doc) -> None:
    """A voided check's number is never reused."""
    from sales.models import Refund

    is_refund = isinstance(doc, Refund)
    ledger = doc.bank_account if is_refund else doc.paid_from
    number = doc.reference if is_refund else doc.check_number
    if doc.method != "CHECK" or not normalize(number):
        return
    bank_account = _bank_account(ledger)
    if bank_account is None:
        return
    row = CheckNumber.objects.filter(bank_account=bank_account, number=normalize(number)).first()
    if row is None:
        record(ledger, number, CheckStatus.VOIDED, vendor_payment=None if is_refund else doc,
               refund=doc if is_refund else None)
    elif row.status != CheckStatus.SPOILED:
        row.status = CheckStatus.VOIDED
        row.save()


def used_numbers(bank_account: BankAccount) -> set[str]:
    used = {normalize(n) for n in CheckNumber.objects.filter(bank_account=bank_account).values_list("number", flat=True)}
    used.update(n for n, _ in _legacy_numbers(bank_account.ledger_account))
    return used


def next_number(bank_account: BankAccount) -> int:
    """The account's next check number, skipping any number already used."""
    used = used_numbers(bank_account)
    n = bank_account.next_check_number
    while str(n) in used:
        n += 1
    return n


def is_to_print(doc) -> bool:
    """A check-method vendor payment or refund with no number yet, still open."""
    from sales.models import DocStatus, Refund

    if doc.method != "CHECK" or doc.status != DocStatus.OPEN:
        return False
    number = doc.reference if isinstance(doc, Refund) else doc.check_number
    return not (number or "").strip()

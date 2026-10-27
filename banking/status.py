"""What the home screen shows about banking."""
from datetime import timedelta

from django.db.models import Min
from django.utils import timezone

from ledger.models import JournalLine

from .models import BankAccount, BankTransaction, ItemStatus, PlaidItem, ReconStatus, TxnStatus

STALE_SYNC = timedelta(hours=24)
RECONCILE_DAYS = 40


def inbox_count() -> int:
    return BankTransaction.objects.filter(status=TxnStatus.NEW).count()


def sync_status(now=None) -> dict:
    """level is 'none' (nothing linked), 'ok', or 'warning'."""
    now = now or timezone.now()
    items = list(PlaidItem.objects.all())
    if not items:
        return {"level": "none", "message": "No banks linked", "needs_login": [], "last_sync": None}
    needs_login = [i for i in items if i.status == ItemStatus.NEEDS_LOGIN]
    synced = [i.last_sync_at for i in items if i.last_sync_at]
    last = max(synced) if synced else None
    if needs_login:
        names = ", ".join(i.institution_name for i in needs_login)
        verb = "needs" if len(needs_login) == 1 else "need"
        message = f"{names} {verb} you to sign in again."
        level = "warning"
    elif last is None:
        message, level = "Not synced yet.", "warning"
    elif now - last > STALE_SYNC:
        message, level = f"Last sync {timezone.localtime(last):%Y-%m-%d %H:%M}. It has been over 24 hours.", "warning"
    else:
        message, level = f"Last sync {timezone.localtime(last):%Y-%m-%d %H:%M}", "ok"
    errors = [i for i in items if i.status == ItemStatus.ERROR]
    if errors and not needs_login:
        message = f"{errors[0].institution_name}: {errors[0].last_error}"
        level = "warning"
    return {"level": level, "message": message, "needs_login": needs_login, "last_sync": last}


def unreconciled_accounts(today=None) -> list[BankAccount]:
    """Active accounts whose last finished reconciliation is over 40 days old. An account that was never reconciled
    counts once it has ledger activity older than that."""
    today = today or timezone.localdate()
    cutoff = today - timedelta(days=RECONCILE_DAYS)
    late = []
    for account in BankAccount.objects.filter(is_active=True).select_related("ledger_account"):
        last = (
            account.reconciliations.filter(status=ReconStatus.FINISHED).order_by("-statement_date").first()
        )
        if last is not None:
            if last.statement_date < cutoff:
                late.append(account)
            continue
        first_line = JournalLine.objects.in_ledger().filter(account=account.ledger_account).aggregate(
            first=Min("entry__date"))["first"]
        if first_line is not None and first_line < cutoff:
            late.append(account)
    return late

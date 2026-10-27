"""Pulls new activity from Plaid for each linked bank. Only posted transactions become inbox rows."""
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import plaid_client
from .crypto import FieldKeyError
from .importers import ParsedTxn, import_transactions
from .models import BankAccount, BankTransaction, ItemStatus, PlaidItem, TxnSource, TxnStatus
from .plaid_client import PlaidError

logger = logging.getLogger(__name__)

REMOVED_REASON = "Removed by the bank."


@dataclass
class SyncResult:
    added: int = 0
    modified: int = 0
    removed: int = 0
    skipped_pending: int = 0


def _apply_page(item: PlaidItem, accounts: dict[str, BankAccount], page, result: SyncResult, user=None) -> None:
    by_account: dict[int, list[ParsedTxn]] = {}

    def queue(t) -> None:
        account = accounts.get(t.account_id)
        if account is None:
            return
        by_account.setdefault(account.pk, []).append(
            ParsedTxn(t.transaction_id, t.date, t.name, t.amount, t.check_number))

    for t in page.added:
        if t.pending:
            result.skipped_pending += 1
        else:
            queue(t)

    for t in page.modified:
        if t.pending:
            result.skipped_pending += 1
            continue
        account = accounts.get(t.account_id)
        row = account and BankTransaction.objects.filter(bank_account=account, external_id=t.transaction_id).first()
        if row is None:
            queue(t)  # a pending row that has now posted arrives as a modification
        elif row.status == TxnStatus.NEW:
            row.date, row.description, row.amount, row.check_number = t.date, t.name, t.amount, t.check_number
            row.save()
            result.modified += 1

    for account in accounts.values():
        if account.pk in by_account:
            saved = import_transactions(account, by_account[account.pk], source=TxnSource.PLAID, user=user)
            result.added += saved.added + saved.possible_duplicates

    for transaction_id in page.removed:
        rows = BankTransaction.objects.filter(bank_account__in=accounts.values(), external_id=transaction_id)
        for row in rows:
            if row.status == TxnStatus.NEW:
                row.delete()
            else:
                row.exclude_reason = REMOVED_REASON
                row.save()
            result.removed += 1


def sync_item(item: PlaidItem) -> SyncResult:
    result = SyncResult()
    try:
        token = item.get_access_token()
        client = plaid_client.get_client()
        accounts = {a.plaid_account_id: a for a in item.accounts.filter(is_active=True) if a.plaid_account_id}
        cursor = item.cursor or None
        with transaction.atomic():
            while True:
                page = client.transactions_sync(token, cursor)
                _apply_page(item, accounts, page, result)
                cursor = page.next_cursor
                if not page.has_more:
                    break
    except FieldKeyError as exc:
        _fail(item, ItemStatus.ERROR, str(exc))
        return SyncResult()
    except PlaidError as exc:
        status = ItemStatus.NEEDS_LOGIN if exc.code == "ITEM_LOGIN_REQUIRED" else ItemStatus.ERROR
        _fail(item, status, exc.message)
        return SyncResult()

    item.cursor = cursor or ""
    item.status = ItemStatus.OK
    item.last_error = ""
    item.last_sync_at = timezone.now()
    item.save()
    logger.info("Synced %s: %s added, %s modified, %s removed.", item.institution_name, result.added,
                result.modified, result.removed)
    return result


def _fail(item: PlaidItem, status: str, message: str) -> None:
    item.status = status
    item.last_error = message
    item.save()
    logger.warning("Sync for %s failed (%s).", item.institution_name, status)


def sync_all() -> None:
    for item in PlaidItem.objects.all():
        try:
            sync_item(item)
        except Exception:  # noqa: BLE001 - one bad bank must not stop the others
            logger.exception("Sync for %s failed unexpectedly.", item.institution_name)
            _fail(item, ItemStatus.ERROR, "The sync failed. Try Sync now, or re-link the account.")

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from banking.crypto import FieldKeyError, decrypt, encrypt
from banking.models import (
    AccountKind, BankAccount, BankTransaction, Match, PlaidItem, TxnSource, TxnStatus,
)
from ledger.models import Account, AccountSubtype, AccountType

D = Decimal


def test_encrypt_decrypt_round_trip(field_key):
    token = encrypt("access-sandbox-abc123")
    assert token != "access-sandbox-abc123"
    assert decrypt(token) == "access-sandbox-abc123"


def test_missing_key_gives_message(settings):
    settings.DNB_FIELD_KEY = ""
    with pytest.raises(FieldKeyError, match="Plaid tokens can't be read. Check DNB_FIELD_KEY or re-link the accounts."):
        encrypt("x")


def test_wrong_key_gives_message(field_key, settings):
    from cryptography.fernet import Fernet

    token = encrypt("secret")
    settings.DNB_FIELD_KEY = Fernet.generate_key().decode()
    with pytest.raises(FieldKeyError, match="Plaid tokens can't be read"):
        decrypt(token)


def test_garbage_key_gives_message(settings):
    settings.DNB_FIELD_KEY = "not-a-key"
    with pytest.raises(FieldKeyError):
        encrypt("x")


def test_access_token_not_in_database_column(db, field_key):
    item = PlaidItem(institution_name="First Bank")
    item.set_access_token("access-sandbox-abc123")
    item.save()
    with connection.cursor() as c:
        c.execute("SELECT access_token FROM banking_plaiditem WHERE id = %s", [item.pk])
        raw = c.fetchone()[0]
    assert "access-sandbox-abc123" not in raw
    assert PlaidItem.objects.get(pk=item.pk).get_access_token() == "access-sandbox-abc123"


def test_audit_log_excludes_access_token(db, field_key):
    from auditlog.models import LogEntry

    item = PlaidItem(institution_name="First Bank")
    item.set_access_token("access-sandbox-abc123")
    item.save()
    entries = LogEntry.objects.get_for_object(item)
    assert entries.count() == 1
    logged = " ".join(str(e.changes) for e in entries)
    assert "access-sandbox-abc123" not in logged
    assert "access_token" not in logged
    assert "First Bank" in logged


@pytest.mark.parametrize("kind,subtype,ok", [
    (AccountKind.CHECKING, AccountSubtype.BANK, True),
    (AccountKind.SAVINGS, AccountSubtype.BANK, True),
    (AccountKind.CARD, AccountSubtype.CREDIT_CARD, True),
    (AccountKind.LOAN, AccountSubtype.OTHER_CURRENT_LIABILITY, True),
    (AccountKind.LOAN, AccountSubtype.LONG_TERM_LIABILITY, True),
    (AccountKind.LOAN, AccountSubtype.CREDIT_CARD, True),
    (AccountKind.LOAN, AccountSubtype.EXPENSE, False),
    (AccountKind.CHECKING, AccountSubtype.CREDIT_CARD, False),
    (AccountKind.CARD, AccountSubtype.BANK, False),
    (AccountKind.LOAN, AccountSubtype.BANK, False),
])
def test_kind_must_fit_ledger_subtype(db, kind, subtype, ok):
    type_ = {AccountSubtype.BANK: AccountType.ASSET, AccountSubtype.EXPENSE: AccountType.EXPENSE}.get(
        subtype, AccountType.LIABILITY)
    acct = Account.objects.create(number="1900", name="Test", type=type_, subtype=subtype)
    ba = BankAccount(ledger_account=acct, kind=kind)
    if ok:
        ba.full_clean()
    else:
        with pytest.raises(ValidationError):
            ba.full_clean()


def test_external_id_unique_per_account(checking, card_account):
    kw = dict(date=date(2026, 1, 5), description="x", amount=D("1.00"), source=TxnSource.FILE)
    BankTransaction.objects.create(bank_account=checking, external_id="A1", **kw)
    BankTransaction.objects.create(bank_account=card_account, external_id="A1", **kw)
    with pytest.raises(IntegrityError), transaction.atomic():
        BankTransaction.objects.create(bank_account=checking, external_id="A1", **kw)


def test_new_transaction_defaults(checking):
    t = BankTransaction.objects.create(
        bank_account=checking, external_id="A1", date=date(2026, 1, 5), description="x",
        amount=D("1.00"), source=TxnSource.PLAID)
    assert t.status == TxnStatus.NEW and t.exclude_reason == "" and t.check_number == ""


def test_match_needs_exactly_one_target(checking, posted_entry, admin_user):
    t = BankTransaction.objects.create(
        bank_account=checking, external_id="A1", date=date(2026, 1, 5), description="x",
        amount=D("1.00"), source=TxnSource.FILE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Match.objects.create(bank_transaction=t, confirmed_by=admin_user)
    m = Match.objects.create(bank_transaction=t, journal_entry=posted_entry, confirmed_by=admin_user)
    assert t.match == m


def test_match_rejects_two_targets(checking, posted_entry, admin_user, customer, sales_settings, bank):
    from sales.models import Deposit

    dep = Deposit.objects.create(number="DEP-1", date=date(2026, 1, 5), bank_account=bank, created_by=admin_user)
    t = BankTransaction.objects.create(
        bank_account=checking, external_id="A1", date=date(2026, 1, 5), description="x",
        amount=D("1.00"), source=TxnSource.FILE)
    with pytest.raises(IntegrityError), transaction.atomic():
        Match.objects.create(bank_transaction=t, journal_entry=posted_entry, deposit=dep, confirmed_by=admin_user)


def test_standard_role_permissions(standard_user):
    perms = {p.split(".")[1] for p in standard_user.get_all_permissions()}
    for model in ("banktransaction", "match", "rule", "transfer", "checknumber"):
        assert {f"add_{model}", f"change_{model}", f"view_{model}"} <= perms
    assert "view_bankaccount" in perms and "add_bankaccount" not in perms
    assert "view_reconciliation" in perms and "change_reconciliation" not in perms


def test_admin_role_has_banking(admin_user):
    assert admin_user.has_perm("banking.add_bankaccount")

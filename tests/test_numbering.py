import threading

import pytest
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, connection, transaction

from sales.errors import SalesError
from sales.models import DocType, Invoice, Refund
from sales.numbering import next_number, set_sequence


def test_numbers_are_sequential_and_padded(db):
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000001"
        assert next_number(DocType.INV) == "INV-000002"
        assert next_number(DocType.SO) == "SO-00001"


def test_each_type_has_its_own_sequence(db):
    with transaction.atomic():
        assert [next_number(t) for t in (DocType.PMT, DocType.CN, DocType.DEP, DocType.REF)] == [
            "PMT-00001", "CN-00001", "DEP-00001", "REF-00001"]


def test_rolled_back_save_returns_the_number(db):
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            assert next_number(DocType.INV) == "INV-000001"
            raise RuntimeError("save failed")
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000001"


def test_next_number_needs_a_transaction(transactional_db):
    with pytest.raises(RuntimeError):
        next_number(DocType.INV)


@pytest.mark.django_db(transaction=True)
def test_concurrent_numbers_are_unique(ledger_purge):
    got, errors = [], []

    def worker():
        try:
            for _ in range(20):
                with transaction.atomic():
                    got.append(next_number(DocType.INV))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(got) == 40 and len(set(got)) == 40


def _invoice(customer, admin_user, number):
    from datetime import date

    return Invoice.objects.create(
        number=number, customer=customer, date=date(2026, 10, 1), due_date=date(2026, 10, 31), created_by=admin_user)


def test_set_sequence_refuses_used_number(admin_user, customer):
    _invoice(customer, admin_user, "INV-000005")
    with pytest.raises(SalesError, match="INV-000005 is already used. Choose a higher next number."):
        set_sequence(DocType.INV, prefix="INV-", next_number=5, user=admin_user)
    seq = set_sequence(DocType.INV, prefix="INV-", next_number=6, user=admin_user)
    assert seq.next_number == 6
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000006"


def test_set_sequence_refuses_going_below_a_used_number(admin_user, customer):
    _invoice(customer, admin_user, "INV-000005")
    with pytest.raises(SalesError, match="INV-000005 is already used."):
        set_sequence(DocType.INV, prefix="INV-", next_number=3, user=admin_user)


def test_set_sequence_new_prefix_is_free(admin_user, customer):
    _invoice(customer, admin_user, "INV-000005")
    seq = set_sequence(DocType.INV, prefix="ZB-", next_number=100, user=admin_user)
    with transaction.atomic():
        assert next_number(DocType.INV) == "ZB-000100"
    assert seq.prefix == "ZB-"


def test_set_sequence_admin_only(standard_user):
    with pytest.raises(PermissionDenied):
        set_sequence(DocType.INV, prefix="INV-", next_number=10, user=standard_user)


def test_next_number_skips_a_number_already_taken(admin_user, customer):
    _invoice(customer, admin_user, "INV-000001")
    with transaction.atomic():
        assert next_number(DocType.INV) == "INV-000002"

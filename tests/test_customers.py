from datetime import date

import pytest
from django.core.exceptions import ValidationError

from contacts.models import Customer, Terms, due_date


def test_net_30_due_date():
    assert due_date(Terms.NET_30, date(2026, 10, 27)) == date(2026, 11, 26)


def test_due_on_receipt_is_same_day():
    assert due_date(Terms.DUE_ON_RECEIPT, date(2026, 10, 27)) == date(2026, 10, 27)


def test_default_terms_are_net_30(db):
    assert Customer.objects.create(name="Acme").terms == Terms.NET_30


def test_duplicate_name_any_case_is_rejected(customer):
    with pytest.raises(ValidationError, match="A customer named acme builders already exists."):
        Customer.objects.create(name="acme builders")


def test_editing_a_customer_is_not_a_duplicate(customer):
    customer.phone = "555-0100"
    customer.save()


def test_customer_changes_are_audited(customer):
    from auditlog.models import LogEntry

    assert LogEntry.objects.get_for_object(customer).exists()


def test_standard_can_add_change_view_customers(standard_user):
    for action in ("add", "change", "view"):
        assert standard_user.has_perm(f"contacts.{action}_customer")
    assert not standard_user.has_perm("contacts.delete_customer")


def test_admin_has_contacts_and_sales_permissions(admin_user):
    assert admin_user.has_perm("contacts.delete_customer")

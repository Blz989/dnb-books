import pytest
from django.core.exceptions import ValidationError

from contacts.models import Vendor, mask_tax_id


def test_duplicate_name_any_case_is_rejected(vendor):
    with pytest.raises(ValidationError, match="A vendor named acme supply already exists."):
        Vendor.objects.create(name="acme supply")


def test_editing_a_vendor_is_not_a_duplicate(vendor):
    vendor.phone = "555-0100"
    vendor.save()


def test_find_by_name_ignores_case_and_spacing(vendor):
    assert Vendor.find_by_name("  acme   supply ") == vendor
    assert Vendor.find_by_name("Nobody") is None


def test_mask_tax_id():
    assert mask_tax_id("12-3456789") == "•••-••-6789"
    assert mask_tax_id("") == ""


def test_default_expense_account_must_be_an_expense(vendor, revenue):
    vendor.default_expense_account = revenue
    with pytest.raises(ValidationError, match="Choose an expense account."):
        vendor.save()


def test_default_expense_account_accepts_expense(vendor, expense):
    vendor.default_expense_account = expense
    vendor.save()


def test_standard_user_can_manage_vendors(standard_user):
    assert standard_user.has_perm("contacts.add_vendor")
    assert standard_user.has_perm("contacts.change_vendor")
    assert standard_user.has_perm("contacts.view_vendor")


def test_extra_spaces_do_not_make_a_different_vendor(vendor):
    with pytest.raises(ValidationError, match="A vendor named acme supply already exists."):
        Vendor.objects.create(name="  acme   supply ")

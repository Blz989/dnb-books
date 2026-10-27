import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command

from core.roles import is_admin, require_admin


def test_is_admin(admin_user, standard_user):
    assert is_admin(admin_user) and not is_admin(standard_user)


def test_require_admin_denies_standard(standard_user):
    with pytest.raises(PermissionDenied, match="Only an admin"):
        require_admin(standard_user)


def test_setup_roles_idempotent(db):
    call_command("setup_roles")
    call_command("setup_roles")
    assert Group.objects.filter(name__in=["Admin", "Standard"]).count() == 2


def test_standard_role_permissions_on_ledger(db):
    call_command("setup_roles")
    perms = set(Group.objects.get(name="Standard").permissions.values_list("codename", flat=True))
    assert {"add_journalentry", "view_accountingperiod"} <= perms
    assert "add_accountingperiod" not in perms and "delete_journalentry" not in perms

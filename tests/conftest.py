import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command

from core.roles import ADMIN_GROUP, STANDARD_GROUP


def _user(username, group_name):
    call_command("setup_roles")
    user = get_user_model().objects.create_user(username, password="pw", is_staff=True)
    user.groups.add(Group.objects.get(name=group_name))
    return user


@pytest.fixture
def standard_user(db):
    return _user("standard", STANDARD_GROUP)


@pytest.fixture
def admin_user(db):
    return _user("admin", ADMIN_GROUP)

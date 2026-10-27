import pytest
from django.contrib.auth import get_user_model


@pytest.fixture
def standard_user(db):
    return get_user_model().objects.create_user("standard", password="pw", is_staff=True)


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_user("admin", password="pw", is_staff=True)

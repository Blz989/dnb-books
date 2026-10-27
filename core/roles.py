from django.core.exceptions import PermissionDenied

ADMIN_GROUP = "Admin"
STANDARD_GROUP = "Standard"


def is_admin(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    return user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists()


def require_admin(user) -> None:
    if not is_admin(user):
        raise PermissionDenied("Only an admin can do this.")

from core.roles import is_admin


def nav(request):
    """Sidebar data: admin flag and how many items are below zero."""
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"negative_stock_count": 0, "is_admin": False}
    from items.reports import negative_item_count

    return {"negative_stock_count": negative_item_count(), "is_admin": is_admin(user)}

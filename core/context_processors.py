from core.roles import is_admin


def nav(request):
    """Sidebar data. negative_stock_count is wired to real stock in a later task."""
    return {"negative_stock_count": 0, "is_admin": is_admin(getattr(request, "user", None))}

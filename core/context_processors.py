from django.conf import settings

from core.roles import is_admin


def nav(request):
    """Sidebar data: admin flag, how many items are below zero, the company profile, and the reports list."""
    from sales.models import SalesSettings

    config = SalesSettings.objects.filter(pk=1).only("company_name", "logo").first()
    brand = {"name": (config.company_name if config else "") or "DNB Books", "has_logo": bool(config and config.logo)}
    user = getattr(request, "user", None)
    if not getattr(user, "is_authenticated", False):
        return {"negative_stock_count": 0, "is_admin": False, "brand": brand}
    from items.reports import negative_item_count

    out = {"negative_stock_count": negative_item_count(), "is_admin": is_admin(user), "brand": brand}
    match = getattr(request, "resolver_match", None)
    if match and match.namespace == "reports":
        from core.report_catalog import catalog

        out["report_groups"] = catalog(match.url_name)
        query = request.GET.copy()
        query["format"] = "pdf"
        out["pdf_url"] = f"{request.path}?{query.urlencode()}"
        out["pdf"] = getattr(request, "report_pdf", False)
        if out["pdf"]:
            from core.company import logo_uri

            out["pdf_logo_uri"] = logo_uri()
            out["pdf_font_uri"] = (settings.BASE_DIR / "core" / "static" / "core" / "fonts").as_uri()
    return out

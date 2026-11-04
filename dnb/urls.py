from django.contrib import admin
from django.urls import include, path

from core.views import home
from sales.views.settings import logo as sales_logo

urlpatterns = [
    path("", home, name="home"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("customers/", include("contacts.urls")),
    path("items/", include("items.urls")),
    path("sales/", include("sales.urls")),
    path("vendors/", include("purchasing.vendor_urls")),
    path("purchasing/", include("purchasing.urls")),
    path("handling/", include("handling.urls")),
    path("banking/", include("banking.urls")),
    path("reports/", include("ledger.urls")),
    path("books/", include("ledger.books_urls")),
    path("settings/", include("core.urls")),
    path("brand/logo/", sales_logo, name="brand-logo"),
    path("admin/", admin.site.urls),
]

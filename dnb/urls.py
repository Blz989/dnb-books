from django.contrib import admin
from django.urls import include, path

from core.views import home

urlpatterns = [
    path("", home, name="home"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("customers/", include("contacts.urls")),
    path("items/", include("items.urls")),
    path("sales/", include("sales.urls")),
    path("reports/", include("ledger.urls")),
    path("admin/", admin.site.urls),
]

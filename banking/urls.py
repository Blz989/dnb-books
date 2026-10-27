from django.urls import path

from .views import accounts

app_name = "banking"

urlpatterns = [
    path("", accounts.account_list, name="accounts"),
    path("new/", accounts.account_new, name="account-new"),
    path("<int:pk>/edit/", accounts.account_edit, name="account-edit"),
    path("<int:pk>/import/", accounts.import_file, name="import-file"),
    path("<int:pk>/import/map/", accounts.import_map, name="import-map"),
    path("<int:pk>/import/result/", accounts.import_result, name="import-result"),
    path("link/", accounts.link_page, name="link"),
    path("link/token/", accounts.link_token, name="link-token"),
    path("link/exchange/", accounts.link_exchange, name="link-exchange"),
    path("link/<int:pk>/update/", accounts.link_update_page, name="link-update"),
    path("link/<int:pk>/update/token/", accounts.link_update_token, name="link-update-token"),
    path("link/<int:pk>/relinked/", accounts.link_relinked, name="link-relinked"),
    path("link/<int:pk>/map/", accounts.link_map, name="link-map"),
    path("sync/<int:pk>/", accounts.sync_now, name="sync-now"),
]

from django.urls import path

from .views import accounts, inbox, transfers

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
    path("inbox/", inbox.inbox_list, name="inbox"),
    path("inbox/bulk/", inbox.bulk_confirm, name="inbox-bulk"),
    path("inbox/<int:pk>/confirm/", inbox.confirm_row, name="inbox-confirm"),
    path("inbox/<int:pk>/change/", inbox.change_row, name="inbox-change"),
    path("inbox/<int:pk>/exclude/", inbox.exclude_row, name="inbox-exclude"),
    path("inbox/<int:pk>/restore/", inbox.restore_row, name="inbox-restore"),
    path("rules/", inbox.rule_list, name="rules"),
    path("rules/new/", inbox.rule_new, name="rule-new"),
    path("rules/<int:pk>/edit/", inbox.rule_edit, name="rule-edit"),
    path("rules/<int:pk>/toggle/", inbox.rule_toggle, name="rule-toggle"),
    path("rules/<int:pk>/move/", inbox.rule_move, name="rule-move"),
    path("transfers/", transfers.transfer_list, name="transfers"),
    path("transfers/new/", transfers.transfer_new, name="transfer-new"),
    path("transfers/<int:pk>/void/", transfers.transfer_void, name="transfer-void"),
]

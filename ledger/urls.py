from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("trial-balance/", views.trial_balance, name="trial-balance"),
    path("profit-and-loss/", views.profit_and_loss, name="profit-and-loss"),
    path("balance-sheet/", views.balance_sheet, name="balance-sheet"),
    path("general-ledger/", views.general_ledger, name="general-ledger"),
]

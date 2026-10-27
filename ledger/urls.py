from django.urls import path

from items import views as item_views

from . import views

app_name = "reports"

urlpatterns = [
    path("trial-balance/", views.trial_balance, name="trial-balance"),
    path("profit-and-loss/", views.profit_and_loss, name="profit-and-loss"),
    path("balance-sheet/", views.balance_sheet, name="balance-sheet"),
    path("general-ledger/", views.general_ledger, name="general-ledger"),
    path("inventory-valuation/", item_views.inventory_valuation, name="inventory-valuation"),
    path("stock-movements/", item_views.stock_movements, name="stock-movements"),
    path("negative-stock/", item_views.negative_stock, name="negative-stock"),
]

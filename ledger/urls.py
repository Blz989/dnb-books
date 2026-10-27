from django.urls import path

from items import views as item_views

from purchasing.views import reports as purchasing_reports
from sales.views import reports as sales_reports

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
    path("ar-aging/", sales_reports.ar_aging, name="ar-aging"),
    path("open-invoices/", sales_reports.open_invoices, name="open-invoices"),
    path("customer-balances/", sales_reports.customer_balances, name="customer-balances"),
    path("open-sales-orders/", sales_reports.open_sales_orders, name="open-sales-orders"),
    path("undeposited-funds/", sales_reports.undeposited_funds, name="undeposited-funds"),
    path("drop-ship-awaiting-bill/", sales_reports.drop_ship_awaiting_bill, name="drop-ship-awaiting-bill"),
    path("ap-aging/", purchasing_reports.ap_aging, name="ap-aging"),
    path("bills-due/", purchasing_reports.bills_due, name="bills-due"),
    path("vendor-balances/", purchasing_reports.vendor_balances, name="vendor-balances"),
    path("open-purchase-orders/", purchasing_reports.open_purchase_orders, name="open-purchase-orders"),
    path("1099-summary/", purchasing_reports.summary_1099, name="1099-summary"),
]

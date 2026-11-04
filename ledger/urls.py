from django.urls import path

from banking.views import forecast as forecast_views
from core.report_pdf import pdf_export
from handling import views as handling_views
from items import views as item_views
from purchasing.views import reports as purchasing_reports
from sales.views import reports as sales_reports

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("trial-balance/", pdf_export(views.trial_balance), name="trial-balance"),
    path("profit-and-loss/", pdf_export(views.profit_and_loss), name="profit-and-loss"),
    path("balance-sheet/", pdf_export(views.balance_sheet), name="balance-sheet"),
    path("general-ledger/", pdf_export(views.general_ledger), name="general-ledger"),
    path("inventory-valuation/", pdf_export(item_views.inventory_valuation), name="inventory-valuation"),
    path("stock-movements/", pdf_export(item_views.stock_movements), name="stock-movements"),
    path("fifo-lots/", pdf_export(item_views.fifo_lots), name="fifo-lots"),
    path("negative-stock/", pdf_export(item_views.negative_stock), name="negative-stock"),
    path("low-stock/", pdf_export(item_views.low_stock), name="low-stock"),
    path("ar-aging/", pdf_export(sales_reports.ar_aging), name="ar-aging"),
    path("unpaid-invoices/", pdf_export(sales_reports.unpaid_invoices), name="unpaid-invoices"),
    path("open-invoices/", pdf_export(sales_reports.open_invoices), name="open-invoices"),
    path("customer-balances/", pdf_export(sales_reports.customer_balances), name="customer-balances"),
    path("open-sales-orders/", pdf_export(sales_reports.open_sales_orders), name="open-sales-orders"),
    path("undeposited-funds/", pdf_export(sales_reports.undeposited_funds), name="undeposited-funds"),
    path("drop-ship-awaiting-bill/", pdf_export(sales_reports.drop_ship_awaiting_bill), name="drop-ship-awaiting-bill"),
    path("ap-aging/", pdf_export(purchasing_reports.ap_aging), name="ap-aging"),
    path("bills-due/", pdf_export(purchasing_reports.bills_due), name="bills-due"),
    path("vendor-balances/", pdf_export(purchasing_reports.vendor_balances), name="vendor-balances"),
    path("open-purchase-orders/", pdf_export(purchasing_reports.open_purchase_orders), name="open-purchase-orders"),
    path("order-profitability/", pdf_export(handling_views.order_profitability), name="order-profitability"),
    path("cash-forecast/", pdf_export(forecast_views.cash_forecast), name="cash-forecast"),
    path("1099-summary/", pdf_export(purchasing_reports.summary_1099), name="1099-summary"),
]

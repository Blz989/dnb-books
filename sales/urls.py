from django.urls import path

from .views import deposits, invoices, orders, receipts, settings as settings_views

app_name = "sales"

urlpatterns = [
    path("settings/", settings_views.sales_settings, name="settings"),
    path("settings/logo/", settings_views.logo, name="logo"),
    path("orders/", orders.order_list, name="order-list"),
    path("orders/new/", orders.order_new, name="order-new"),
    path("orders/<str:number>/", orders.order_detail, name="order-detail"),
    path("orders/<str:number>/edit/", orders.order_edit, name="order-edit"),
    path("orders/<str:number>/invoice/", invoices.order_invoice, name="order-invoice"),
    path("orders/<str:number>/close/", orders.order_close, name="order-close"),
    path("invoices/", invoices.invoice_list, name="invoice-list"),
    path("invoices/new/", invoices.invoice_new, name="invoice-new"),
    path("invoices/<str:number>/", invoices.invoice_detail, name="invoice-detail"),
    path("invoices/<str:number>/edit/", invoices.invoice_edit, name="invoice-edit"),
    path("invoices/<str:number>/pdf/", invoices.invoice_pdf_view, name="invoice-pdf"),
    path("invoices/<str:number>/void/", invoices.invoice_void, name="invoice-void"),
    path("invoices/<str:number>/revisions/<int:revision>/", invoices.invoice_revision, name="invoice-revision"),
    path("invoices/<str:number>/applications/<str:kind>/<int:pk>/remove/", invoices.application_remove, name="application-remove"),
    path("payments/", receipts.payment_list, name="payment-list"),
    path("payments/new/", receipts.payment_new, name="payment-new"),
    path("payments/<str:number>/", receipts.payment_detail, name="payment-detail"),
    path("payments/<str:number>/apply/", receipts.payment_apply, name="payment-apply"),
    path("payments/<str:number>/void/", receipts.payment_void, name="payment-void"),
    path("deposits/", deposits.deposit_list, name="deposit-list"),
    path("deposits/new/", deposits.deposit_new, name="deposit-new"),
    path("deposits/<str:number>/", deposits.deposit_detail, name="deposit-detail"),
    path("deposits/<str:number>/void/", deposits.deposit_void, name="deposit-void"),
    path("credits/", receipts.credit_list, name="credit-list"),
    path("credits/new/", receipts.credit_new, name="credit-new"),
    path("credits/<str:number>/", receipts.credit_detail, name="credit-detail"),
    path("credits/<str:number>/apply/", receipts.credit_apply, name="credit-apply"),
    path("credits/<str:number>/void/", receipts.credit_void, name="credit-void"),
    path("refunds/new/", receipts.refund_new, name="refund-new"),
    path("refunds/<str:number>/", receipts.refund_detail, name="refund-detail"),
    path("refunds/<str:number>/void/", receipts.refund_void, name="refund-void"),
]

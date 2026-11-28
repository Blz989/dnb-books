from django.urls import path

import core.urlnumbers  # noqa: F401  (registers the docnum converter)

from .views import deposits, invoices, orders, receipts, settings as settings_views, statements

app_name = "sales"

urlpatterns = [
    path("settings/", settings_views.sales_settings, name="settings"),
    path("settings/logo/", settings_views.logo, name="logo"),
    path("orders/", orders.order_list, name="order-list"),
    path("orders/new/", orders.order_new, name="order-new"),
    path("orders/<docnum:number>/", orders.order_detail, name="order-detail"),
    path("orders/<docnum:number>/edit/", orders.order_edit, name="order-edit"),
    path("orders/<docnum:number>/invoice/", invoices.order_invoice, name="order-invoice"),
    path("orders/<docnum:number>/close/", orders.order_close, name="order-close"),
    path("orders/<docnum:number>/delete/", orders.order_delete, name="order-delete"),
    path("invoices/", invoices.invoice_list, name="invoice-list"),
    path("invoices/new/", invoices.invoice_new, name="invoice-new"),
    path("invoices/<docnum:number>/", invoices.invoice_detail, name="invoice-detail"),
    path("invoices/<docnum:number>/edit/", invoices.invoice_edit, name="invoice-edit"),
    path("invoices/<docnum:number>/pdf/", invoices.invoice_pdf_view, name="invoice-pdf"),
    path("invoices/<docnum:number>/void/", invoices.invoice_void, name="invoice-void"),
    path("invoices/<docnum:number>/write-off/", invoices.invoice_write_off, name="invoice-write-off"),
    path("invoices/<docnum:number>/delete/", invoices.invoice_delete, name="invoice-delete"),
    path("invoices/<docnum:number>/revisions/<int:revision>/", invoices.invoice_revision, name="invoice-revision"),
    path("invoices/<docnum:number>/applications/<str:kind>/<int:pk>/remove/", invoices.application_remove, name="application-remove"),
    path("payments/", receipts.payment_list, name="payment-list"),
    path("payments/new/", receipts.payment_new, name="payment-new"),
    path("payments/<docnum:number>/", receipts.payment_detail, name="payment-detail"),
    path("payments/<docnum:number>/apply/", receipts.payment_apply, name="payment-apply"),
    path("payments/<docnum:number>/void/", receipts.payment_void, name="payment-void"),
    path("deposits/", deposits.deposit_list, name="deposit-list"),
    path("deposits/new/", deposits.deposit_new, name="deposit-new"),
    path("deposits/<docnum:number>/", deposits.deposit_detail, name="deposit-detail"),
    path("deposits/<docnum:number>/void/", deposits.deposit_void, name="deposit-void"),
    path("credits/", receipts.credit_list, name="credit-list"),
    path("credits/new/", receipts.credit_new, name="credit-new"),
    path("credits/<docnum:number>/", receipts.credit_detail, name="credit-detail"),
    path("credits/<docnum:number>/apply/", receipts.credit_apply, name="credit-apply"),
    path("credits/<docnum:number>/void/", receipts.credit_void, name="credit-void"),
    path("customers/<int:pk>/statement/", statements.statement, name="statement"),
    path("customers/<int:pk>/statement/pdf/", statements.statement_pdf_view, name="statement-pdf"),
    path("refunds/new/", receipts.refund_new, name="refund-new"),
    path("refunds/<docnum:number>/", receipts.refund_detail, name="refund-detail"),
    path("refunds/<docnum:number>/void/", receipts.refund_void, name="refund-void"),
]

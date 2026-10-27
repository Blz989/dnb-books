from django.urls import path

from .views import invoices, orders, settings as settings_views

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
]

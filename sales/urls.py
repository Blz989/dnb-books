from django.urls import path

from .views import invoices, settings as settings_views

app_name = "sales"

urlpatterns = [
    path("settings/", settings_views.sales_settings, name="settings"),
    path("settings/logo/", settings_views.logo, name="logo"),
    path("invoices/<str:number>/pdf/", invoices.invoice_pdf_view, name="invoice-pdf"),
]

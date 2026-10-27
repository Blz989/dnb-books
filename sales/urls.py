from django.urls import path

from .views import invoices

app_name = "sales"

urlpatterns = [
    path("invoices/<str:number>/pdf/", invoices.invoice_pdf_view, name="invoice-pdf"),
]

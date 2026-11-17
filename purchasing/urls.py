from django.urls import path

import core.urlnumbers  # noqa: F401  (registers the docnum converter)

from .views import bills, credits, orders, payments, settings

app_name = "purchasing"

urlpatterns = [
    path("settings/", settings.purchasing_settings, name="settings"),
    path("orders/", orders.po_list, name="po-list"),
    path("orders/new/", orders.po_new, name="po-new"),
    path("orders/drop-ship/", orders.drop_ship_po_multi, name="drop-ship-po-multi"),
    path("orders/drop-ship/<docnum:number>/", orders.drop_ship_po, name="drop-ship-po"),
    path("orders/<docnum:number>/pdf/", orders.po_pdf_view, name="po-pdf"),
    path("orders/<docnum:number>/", orders.po_detail, name="po-detail"),
    path("orders/<docnum:number>/edit/", orders.po_edit, name="po-edit"),
    path("orders/<docnum:number>/bill/", bills.po_bill, name="po-bill"),
    path("orders/<docnum:number>/close/", orders.po_close, name="po-close"),
    path("orders/<docnum:number>/delete/", orders.po_delete, name="po-delete"),
    path("bills/", bills.bill_list, name="bill-list"),
    path("bills/new/", bills.bill_new, name="bill-new"),
    path("bills/<docnum:number>/", bills.bill_detail, name="bill-detail"),
    path("bills/<docnum:number>/edit/", bills.bill_edit, name="bill-edit"),
    path("bills/<docnum:number>/void/", bills.bill_void, name="bill-void"),
    path("bills/<docnum:number>/delete/", bills.bill_delete, name="bill-delete"),
    path("bills/<docnum:number>/revisions/<int:revision>/", bills.bill_revision, name="bill-revision"),
    path("bills/<docnum:number>/applications/<str:kind>/<int:pk>/remove/", bills.application_remove,
         name="application-remove"),
]

urlpatterns += [
    path("pay/", payments.pay_bills_view, name="pay-bills"),
    path("pay/done/", payments.pay_done, name="pay-done"),
    path("payments/", payments.payment_list, name="payment-list"),
    path("payments/new/", payments.payment_new, name="payment-new"),
    path("payments/<docnum:number>/", payments.payment_detail, name="payment-detail"),
    path("payments/<docnum:number>/apply/", payments.payment_apply, name="payment-apply"),
    path("payments/<docnum:number>/void/", payments.payment_void, name="payment-void"),
    path("credits/", credits.credit_list, name="credit-list"),
    path("credits/new/", credits.credit_new, name="credit-new"),
    path("credits/<docnum:number>/", credits.credit_detail, name="credit-detail"),
    path("credits/<docnum:number>/apply/", credits.credit_apply, name="credit-apply"),
    path("credits/<docnum:number>/void/", credits.credit_void, name="credit-void"),
]

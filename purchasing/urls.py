from django.urls import path

from .views import bills, orders, settings

app_name = "purchasing"

urlpatterns = [
    path("settings/", settings.purchasing_settings, name="settings"),
    path("orders/", orders.po_list, name="po-list"),
    path("orders/new/", orders.po_new, name="po-new"),
    path("orders/drop-ship/<str:number>/", orders.drop_ship_po, name="drop-ship-po"),
    path("orders/<str:number>/", orders.po_detail, name="po-detail"),
    path("orders/<str:number>/edit/", orders.po_edit, name="po-edit"),
    path("orders/<str:number>/bill/", bills.po_bill, name="po-bill"),
    path("orders/<str:number>/close/", orders.po_close, name="po-close"),
    path("bills/", bills.bill_list, name="bill-list"),
    path("bills/new/", bills.bill_new, name="bill-new"),
    path("bills/<str:number>/", bills.bill_detail, name="bill-detail"),
    path("bills/<str:number>/edit/", bills.bill_edit, name="bill-edit"),
    path("bills/<str:number>/void/", bills.bill_void, name="bill-void"),
    path("bills/<str:number>/revisions/<int:revision>/", bills.bill_revision, name="bill-revision"),
    path("bills/<str:number>/applications/<str:kind>/<int:pk>/remove/", bills.application_remove,
         name="application-remove"),
]

from django.contrib import admin

from .models import Bill, PurchaseOrder, PurchasingSettings, Threshold1099, VendorCredit, VendorPayment

for _model in (PurchasingSettings, Threshold1099, PurchaseOrder, Bill, VendorPayment, VendorCredit):
    admin.site.register(_model)

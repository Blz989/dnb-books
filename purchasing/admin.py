from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import Bill, PurchaseOrder, PurchasingSettings, Threshold1099, VendorCredit, VendorPayment

for _model in (PurchasingSettings, Threshold1099):
    admin.site.register(_model)
for _model in (PurchaseOrder, Bill, VendorPayment, VendorCredit):
    admin.site.register(_model, ReadOnlyAdmin)

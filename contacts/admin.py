from django.contrib import admin

from .models import Customer, Vendor


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "terms", "is_active")
    search_fields = ("name", "contact_name", "email")


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "terms", "is_1099", "is_active")
    search_fields = ("name", "contact_name", "email")

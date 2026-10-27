from django.contrib import admin

from .models import Customer, Vendor


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "terms", "is_active")
    search_fields = ("name", "contact_name", "email")


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    """The full tax ID never shows here. It is entered and read on the app's vendor edit form (admin only)."""
    list_display = ("name", "contact_name", "terms", "is_1099", "is_active")
    search_fields = ("name", "contact_name", "email")
    exclude = ("tax_id",)
    readonly_fields = ("tax_id_last_four",)

    @admin.display(description="Tax ID")
    def tax_id_last_four(self, obj):
        return obj.masked_tax_id or "Not entered" if obj and obj.pk else "Enter it on the vendor screen after saving."

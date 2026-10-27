from django.contrib import admin

from .models import Customer


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "contact_name", "terms", "is_active")
    search_fields = ("name", "contact_name", "email")

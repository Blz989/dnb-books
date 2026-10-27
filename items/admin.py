from django.contrib import admin

from .models import Category, InventorySettings

# Setup screens only. Items, movements, and counts are managed on the custom screens.
admin.site.register(Category)
admin.site.register(InventorySettings)

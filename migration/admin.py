from django.contrib import admin

from .models import ImportRun, ZohoRecord


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(ZohoRecord, ReadOnlyAdmin)
admin.site.register(ImportRun, ReadOnlyAdmin)

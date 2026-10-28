from django.contrib import admin

from .models import ImportRun, ZohoRecord


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ImportRunAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "dry_run", "succeeded", "export_name")
    readonly_fields = ("summary",)


admin.site.register(ZohoRecord, ReadOnlyAdmin)
admin.site.register(ImportRun, ImportRunAdmin)

from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import ImportRun, ZohoRecord


class ImportRunAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "dry_run", "succeeded", "export_name")
    readonly_fields = ("summary",)


admin.site.register(ZohoRecord, ReadOnlyAdmin)
admin.site.register(ImportRun, ImportRunAdmin)

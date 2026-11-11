from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    """Visible in the admin, never editable: documents change only through their own screens so the ledger stays in step."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

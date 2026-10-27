from django.contrib import admin

from .models import DocumentSequence, SalesSettings

# Setup screens only. Documents are managed on the sales screens.
admin.site.register(SalesSettings)
admin.site.register(DocumentSequence)

from django.urls import path

from .views import settings

app_name = "purchasing"

urlpatterns = [
    path("settings/", settings.purchasing_settings, name="settings"),
]

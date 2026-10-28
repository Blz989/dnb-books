from django.urls import path

from . import views

app_name = "handling"

urlpatterns = [
    path("settings/", views.handling_settings, name="settings"),
]

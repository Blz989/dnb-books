from django.urls import path

from . import views

app_name = "backup"

urlpatterns = [
    path("run/", views.run_now, name="run"),
    path("status/", views.status, name="status"),
]

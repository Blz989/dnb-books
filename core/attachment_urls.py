from django.urls import path

from . import attachment_views as views

app_name = "attachments"

urlpatterns = [
    path("add/<slug:kind>/<int:pk>/", views.add, name="add"),
    path("<int:pk>/", views.open_file, name="open"),
    path("<int:pk>/remove/", views.remove, name="remove"),
]

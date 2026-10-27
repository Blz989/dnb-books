from django.urls import path

from . import views

app_name = "items"

urlpatterns = [
    path("", views.item_list, name="list"),
    path("new/", views.item_new, name="new"),
    path("<str:sku>/", views.item_detail, name="detail"),
    path("<str:sku>/edit/", views.item_edit, name="edit"),
    path("<str:sku>/deactivate/", views.item_deactivate, name="deactivate"),
]

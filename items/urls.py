from django.urls import path

from . import views

app_name = "items"

urlpatterns = [
    path("", views.item_list, name="list"),
    path("new/", views.item_new, name="new"),
    path("counts/", views.count_list, name="counts"),
    path("counts/new/", views.count_start, name="count-start"),
    path("counts/<int:pk>/", views.count_enter, name="count-enter"),
    path("counts/<int:pk>/review/", views.count_review, name="count-review"),
    path("counts/<int:pk>/cancel/", views.count_cancel, name="count-cancel"),
    path("adjust/", views.adjust, name="adjust"),
    path("adjustments/", views.adjustment_list, name="adjustments"),
    path("movements/<int:pk>/void/", views.void_movement_view, name="void-movement"),
    path("<str:sku>/", views.item_detail, name="detail"),
    path("<str:sku>/edit/", views.item_edit, name="edit"),
    path("<str:sku>/value-adjust/", views.value_adjust, name="value-adjust"),
    path("<str:sku>/deactivate/", views.item_deactivate, name="deactivate"),
]

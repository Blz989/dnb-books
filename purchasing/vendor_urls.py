from django.urls import path

from .views import vendors

app_name = "vendors"

urlpatterns = [
    path("", vendors.vendor_list, name="list"),
    path("new/", vendors.vendor_new, name="new"),
    path("<int:pk>/", vendors.vendor_detail, name="detail"),
    path("<int:pk>/edit/", vendors.vendor_edit, name="edit"),
    path("<int:pk>/deactivate/", vendors.vendor_deactivate, name="deactivate"),
]

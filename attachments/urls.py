from django.urls import path

from . import views

app_name = "attachments"

urlpatterns = [
    path("<int:pk>/", views.serve, name="serve"),
    path("<int:pk>/remove/", views.remove_view, name="remove"),
    path("add/<str:kind>/<str:number>/", views.add, name="add"),
]

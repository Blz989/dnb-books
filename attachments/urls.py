from django.urls import path

from . import views

app_name = "attachments"

urlpatterns = [
    path("<int:pk>/", views.serve, name="serve"),
]

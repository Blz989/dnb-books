from datetime import date

from django.urls import path, register_converter

from . import views

app_name = "handling"


class IsoDateConverter:
    regex = r"\d{4}-\d{2}-\d{2}"

    def to_python(self, value):
        return date.fromisoformat(value)  # a ValueError (Feb 30) means no match, so a 404

    def to_url(self, value):
        return value.isoformat() if isinstance(value, date) else value


register_converter(IsoDateConverter, "isodate")

urlpatterns = [
    path("", views.run_list, name="runs"),
    path("preview/<isodate:period_end>/", views.run_preview, name="preview"),
    path("runs/<int:pk>/", views.run_detail, name="run"),
    path("runs/<int:pk>/void/", views.run_void, name="run-void"),
    path("runs/<int:pk>/statement.pdf", views.run_statement, name="statement"),
    path("settings/", views.handling_settings, name="settings"),
]

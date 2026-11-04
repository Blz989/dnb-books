from django.urls import path

from mcpserver import views as mcp_views

from . import audit_views
from . import settings_views as views

app_name = "settings"

urlpatterns = [
    path("", views.index, name="index"),
    path("company/", views.company_profile, name="company"),
    path("users/", views.users, name="users"),
    path("users/new/", views.user_new, name="user-new"),
    path("users/<int:pk>/", views.user_edit, name="user-edit"),
    path("audit-log/", audit_views.audit_log, name="audit"),
    path("claude/", mcp_views.connections, name="claude"),
    path("advanced/", views.advanced, name="advanced"),
]

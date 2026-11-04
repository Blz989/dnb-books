from django.urls import path

from . import books_views as views

app_name = "books"

urlpatterns = [
    path("journal/", views.journal_list, name="journal"),
    path("journal/new/", views.journal_new, name="journal-new"),
    path("journal/<int:pk>/", views.journal_detail, name="journal-detail"),
    path("journal/<int:pk>/void/", views.journal_void, name="journal-void"),
    path("journal/<int:pk>/post/", views.journal_post_draft, name="journal-post"),
    path("journal/<int:pk>/delete/", views.journal_delete_draft, name="journal-delete"),
    path("accounts/", views.accounts, name="accounts"),
    path("accounts/new/", views.account_new, name="account-new"),
    path("accounts/<int:pk>/", views.account_edit, name="account-edit"),
    path("month-close/", views.periods, name="periods"),
    path("month-close/<str:month>/close/", views.period_close, name="period-close"),
    path("month-close/<str:month>/close-anyway/", views.period_close_anyway, name="period-close-anyway"),
    path("month-close/<str:month>/reopen/", views.period_reopen, name="period-reopen"),
]

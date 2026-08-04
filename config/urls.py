"""URL configuration for the QuizOps project."""

from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import views as account_views

urlpatterns = [
    path("", include("assessments.urls")),
    path("accounts/login/", account_views.ThrottledLoginView.as_view(), name="login"),
    path("accounts/mfa/setup/", account_views.mfa_setup, name="mfa_setup"),
    path("accounts/mfa/verify/", account_views.mfa_verify, name="mfa_verify"),
    path("accounts/me/", account_views.account, name="account"),
    path("accounts/me/export/", account_views.export_my_data, name="export_my_data"),
    path("accounts/me/delete/", account_views.delete_my_account, name="delete_my_account"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Throttled reset views must be registered before django.contrib.auth.urls
    # so they win the name/path match (audit R3-06).
    path(
        "accounts/password_reset/",
        account_views.ThrottledPasswordResetView.as_view(),
        name="password_reset",
    ),
    path(
        "accounts/reset/<uidb64>/<token>/",
        account_views.ThrottledPasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    # Password reset done/complete + password change (finding 35).
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
]

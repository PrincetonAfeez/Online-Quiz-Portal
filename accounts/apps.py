"""Accounts Django app configuration."""

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        from accounts.admin_login import patch_admin_login

        patch_admin_login()

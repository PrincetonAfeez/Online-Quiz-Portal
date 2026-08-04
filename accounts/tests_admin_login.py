"""Tests for throttled Django admin login."""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from accounts.admin_login import patch_admin_login
from accounts.models import User


LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accounts-admin-login-cache",
    }
}


class AdminLoginPatchTests(TestCase):
    def test_patch_admin_login_is_idempotent(self):
        patch_admin_login()
        first = admin.site.login
        patch_admin_login()
        second = admin.site.login
        self.assertIs(first, second)
        self.assertTrue(getattr(admin.site, "_quizops_login_throttled", False))


@override_settings(CACHES=LOCMEM_CACHES, MFA_REQUIRED=False)
class AdminLoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user(
            "admstaff", password="pass-phrase-12", is_staff=True
        )
        self.client = Client()

    def test_admin_login_get_is_not_throttled(self):
        for _ in range(settings.LOGIN_RATE_LIMIT + 3):
            self.client.post(
                "/admin/login/",
                {"username": "admstaff", "password": "wrong-password-xx"},
            )
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)

    def test_admin_login_post_over_limit_returns_429(self):
        for _ in range(settings.LOGIN_RATE_LIMIT):
            self.client.post(
                "/admin/login/",
                {"username": "admstaff", "password": "wrong-password-xx"},
            )
        response = self.client.post(
            "/admin/login/",
            {"username": "admstaff", "password": "pass-phrase-12", "next": "/admin/"},
        )
        self.assertEqual(response.status_code, 429)

    def test_successful_admin_login_refunds_budget(self):
        for _ in range(settings.LOGIN_RATE_LIMIT):
            response = self.client.post(
                "/admin/login/",
                {
                    "username": "admstaff",
                    "password": "pass-phrase-12",
                    "next": "/admin/",
                },
            )
            self.assertEqual(response.status_code, 302)
            self.client.logout()
        response = self.client.post(
            "/admin/login/",
            {"username": "admstaff", "password": "pass-phrase-12", "next": "/admin/"},
        )
        self.assertEqual(response.status_code, 302)

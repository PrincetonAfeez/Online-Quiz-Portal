"""Tests for MFAEnforcementMiddleware."""

from __future__ import annotations

import pyotp
from django.contrib.auth.models import AnonymousUser
from django.test import Client, RequestFactory, TestCase, override_settings

from accounts.mfa import mfa_privilege_epoch, new_totp_secret, SESSION_MFA_EPOCH, SESSION_MFA_USER
from accounts.middleware import MFAEnforcementMiddleware
from accounts.models import Organization, OrganizationMembership, User


class MiddlewareFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="MW Org", slug="mw-org")
        self.admin = User.objects.create_user("mwadmin", password="pass-phrase-12")
        self.student = User.objects.create_user("mwstu", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.client = Client()
        self.factory = RequestFactory()

    def _middleware_response(self, request):
        middleware = MFAEnforcementMiddleware(lambda req: "ok")
        return middleware(request)


@override_settings(MFA_REQUIRED=True)
class MFAEnforcementMiddlewareTests(MiddlewareFixture):
    def test_unauthenticated_passthrough(self):
        request = self.factory.get("/dashboard/")
        request.user = AnonymousUser()
        request.session = self.client.session
        self.assertEqual(self._middleware_response(request), "ok")

    @override_settings(MFA_REQUIRED=False)
    def test_mfa_off_passthrough(self):
        self.client.force_login(self.admin)
        response = self.client.get("/")
        self.assertNotEqual(response.status_code, 302)

    def test_satisfied_session_passthrough(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        session = self.client.session
        session[SESSION_MFA_USER] = self.admin.pk
        session[SESSION_MFA_EPOCH] = mfa_privilege_epoch(self.admin)
        session.save()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_always_allowed_prefixes_passthrough(self):
        self.client.force_login(self.admin)
        for path in (
            "/accounts/logout/",
            "/healthz/",
            "/readyz/",
            "/legal/terms/",
            "/static/app.css",
        ):
            response = self.client.get(path)
            self.assertNotIn("/accounts/mfa/", response.get("Location", ""))

    def test_not_enrolled_redirects_to_setup(self):
        self.client.force_login(self.admin)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/setup/", response["Location"])

    def test_setup_path_allowed_when_not_enrolled(self):
        self.client.force_login(self.admin)
        response = self.client.get("/accounts/mfa/setup/")
        self.assertEqual(response.status_code, 200)

    def test_enrolled_redirects_to_verify(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])

    def test_verify_path_allowed_when_enrolled(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        response = self.client.get("/accounts/mfa/verify/")
        self.assertEqual(response.status_code, 200)

    def test_enrolled_user_blocked_from_setup_by_middleware(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        response = self.client.get("/accounts/mfa/setup/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])

    def test_student_without_mfa_requirement_passthrough(self):
        self.client.force_login(self.student)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

    def test_verify_then_dashboard_allowed(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.post(
            "/accounts/login/",
            {"username": "mwadmin", "password": "pass-phrase-12"},
        )
        code = pyotp.TOTP(secret).now()
        self.client.post("/accounts/mfa/verify/", {"code": code})
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

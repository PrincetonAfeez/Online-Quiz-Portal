"""Tests for accounts views: login, MFA, account, privacy."""

from __future__ import annotations

import json
from datetime import timedelta

import pyotp
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils import timezone

from accounts.mfa import SESSION_MFA_USER, new_totp_secret
from accounts.models import Organization, OrganizationMembership, User
from accounts.views import _auth_throttle_response
from assessments.ratelimit import client_identity
from assessments.models import Attempt, Choice, Question, Quiz, QuizAssignment
from assessments.services import publish_quiz, start_attempt


LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accounts-test-cache",
    }
}


@override_settings(CACHES=LOCMEM_CACHES)
class ViewsFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="View Org", slug="view-org")
        self.admin = User.objects.create_user("viewadmin", password="pass-phrase-12")
        self.student = User.objects.create_user(
            "viewstu", "stu@test.dev", "pass-phrase-12"
        )
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


def _password_reset_confirm_url(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"/accounts/reset/{uid}/{token}/"


@override_settings(MFA_REQUIRED=True)
class ThrottledLoginViewMFATests(ViewsFixture):
    def test_login_redirects_unenrolled_org_admin_to_setup(self):
        response = self.client.post(
            "/accounts/login/",
            {"username": "viewadmin", "password": "pass-phrase-12"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/setup/", response["Location"])

    def test_login_redirects_enrolled_org_admin_to_verify(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        response = self.client.post(
            "/accounts/login/",
            {"username": "viewadmin", "password": "pass-phrase-12"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])

    @override_settings(MFA_REQUIRED=False)
    def test_login_does_not_stamp_mfa_ok_when_enforcement_off(self):
        response = self.client.post(
            "/accounts/login/",
            {"username": "viewadmin", "password": "pass-phrase-12"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.client.session.get(SESSION_MFA_USER))


@override_settings(MFA_REQUIRED=True)
class MFASetupViewTests(ViewsFixture):
    def test_enroll_with_valid_code(self):
        self.client.force_login(self.admin)
        response = self.client.get("/accounts/mfa/setup/")
        self.assertEqual(response.status_code, 200)
        secret = self.client.session.get("mfa_pending_secret")
        self.assertTrue(secret)
        code = pyotp.TOTP(secret).now()
        response = self.client.post("/accounts/mfa/setup/", {"code": code})
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.mfa_enabled)
        self.assertEqual(self.admin.mfa_secret, secret)

    def test_enroll_rejects_bad_code(self):
        self.client.force_login(self.admin)
        self.client.get("/accounts/mfa/setup/")
        response = self.client.post("/accounts/mfa/setup/", {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.mfa_enabled)

    def test_rotate_requires_current_code(self):
        old_secret = new_totp_secret()
        self.admin.mfa_secret = old_secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        self.client.post(
            "/accounts/mfa/verify/",
            {"code": pyotp.TOTP(old_secret).now()},
        )

        self.client.get("/accounts/mfa/setup/")
        new_secret = self.client.session.get("mfa_pending_secret")
        new_code = pyotp.TOTP(new_secret).now()
        response = self.client.post(
            "/accounts/mfa/setup/",
            {"code": new_code, "current_code": "000000"},
        )
        self.assertEqual(response.status_code, 200)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.mfa_secret, old_secret)

    def test_rotate_with_valid_current_and_new_code(self):
        old_secret = new_totp_secret()
        self.admin.mfa_secret = old_secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        self.client.force_login(self.admin)
        code = pyotp.TOTP(old_secret).now()
        self.client.post("/accounts/mfa/verify/", {"code": code})

        self.client.get("/accounts/mfa/setup/")
        new_secret = self.client.session.get("mfa_pending_secret")
        response = self.client.post(
            "/accounts/mfa/setup/",
            {
                "current_code": pyotp.TOTP(old_secret).now(),
                "code": pyotp.TOTP(new_secret).now(),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.mfa_secret, new_secret)


@override_settings(MFA_REQUIRED=True)
class MFAVerifyViewTests(ViewsFixture):
    def setUp(self):
        super().setUp()
        self.secret = new_totp_secret()
        self.admin.mfa_secret = self.secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])

    def test_verify_with_valid_code(self):
        self.client.force_login(self.admin)
        code = pyotp.TOTP(self.secret).now()
        response = self.client.post("/accounts/mfa/verify/", {"code": code})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session.get(SESSION_MFA_USER), self.admin.pk)

    def test_verify_rejects_bad_code(self):
        self.client.force_login(self.admin)
        response = self.client.post("/accounts/mfa/verify/", {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.client.session.get(SESSION_MFA_USER))

    def test_verify_redirects_unenrolled_to_setup(self):
        self.admin.mfa_enabled = False
        self.admin.mfa_secret = ""
        self.admin.save(update_fields=["mfa_enabled", "mfa_secret"])
        self.client.force_login(self.admin)
        response = self.client.get("/accounts/mfa/verify/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/setup/", response["Location"])


class AccountPageTests(ViewsFixture):
    def test_account_page_requires_login(self):
        response = self.client.get("/accounts/me/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_account_page_renders_for_student(self):
        self.client.force_login(self.student)
        response = self.client.get("/accounts/me/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "viewstu")


@override_settings(MFA_REQUIRED=False)
class ExportMyDataTests(ViewsFixture):
    def test_export_returns_json_attachment(self):
        self.client.force_login(self.student)
        response = self.client.post("/accounts/me/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("attachment", response["Content-Disposition"])
        payload = json.loads(response.content)
        self.assertEqual(payload["account"]["username"], "viewstu")
        self.assertIn("organizations", payload)
        self.assertIn("attempts", payload)
        self.assertEqual(payload["format_version"], 1)


class DeleteMyAccountTests(ViewsFixture):
    def _make_assignment(self):
        instructor = User.objects.create_user("delins", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=instructor,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        quiz = Quiz.objects.create(
            organization=self.org, title="Del Quiz", created_by=instructor
        )
        question = Question.objects.create(
            quiz=quiz, prompt="Q", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="y", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="n", is_correct=False, sort_order=2)
        version = publish_quiz(quiz, instructor)
        return QuizAssignment.objects.create(
            organization=self.org,
            title="Del Assignment",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=instructor,
        )

    def test_delete_rejects_bad_confirm(self):
        self.client.force_login(self.student)
        self.client.post(
            "/accounts/me/delete/",
            {"password": "pass-phrase-12", "confirm": "no"},
        )
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_delete_rejects_bad_password(self):
        self.client.force_login(self.student)
        self.client.post(
            "/accounts/me/delete/",
            {"password": "wrong-password-xx", "confirm": "DELETE"},
        )
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_delete_blocked_with_open_attempt(self):
        assignment = self._make_assignment()
        start_attempt(self.student, assignment)
        self.client.force_login(self.student)
        self.client.post(
            "/accounts/me/delete/",
            {"password": "pass-phrase-12", "confirm": "DELETE"},
        )
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertTrue(
            Attempt.objects.filter(
                student=self.student, status=Attempt.Status.IN_PROGRESS
            ).exists()
        )

    def test_delete_success_anonymizes_and_logs_out(self):
        self.client.force_login(self.student)
        response = self.client.post(
            "/accounts/me/delete/",
            {"password": "pass-phrase-12", "confirm": "DELETE"},
        )
        self.assertEqual(response.status_code, 200)
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)
        self.assertTrue(self.student.username.startswith("erased-"))


@override_settings(CACHES=LOCMEM_CACHES, LOGIN_RATE_LIMIT=3)
class PasswordResetConfirmThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            "resetconfirm", "rc@test.dev", "pass-phrase-12"
        )
        self.client = Client()
        self.url = _password_reset_confirm_url(self.user)

    def test_password_reset_confirm_scope_throttles(self):
        for _ in range(settings.LOGIN_RATE_LIMIT):
            request = self.factory.post("/accounts/reset/abc/def/")
            blocked = _auth_throttle_response(
                request, "password_reset_confirm", client_identity(request)
            )
            self.assertIsNone(blocked)
        request = self.factory.post("/accounts/reset/abc/def/")
        blocked = _auth_throttle_response(
            request, "password_reset_confirm", client_identity(request)
        )
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.status_code, 429)

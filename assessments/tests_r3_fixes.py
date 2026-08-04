"""Regressions for Round 3 audit findings R3-01–R3-08."""

from __future__ import annotations

from datetime import timedelta

import pyotp
from django.contrib.admin.models import LogEntry
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from accounts.admin import reset_user_mfa
from accounts.mfa import mfa_privilege_epoch, new_totp_secret
from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Choice,
    ItemPool,
    Question,
    Quiz,
    QuizAssignment,
    Response,
)
from assessments.ratelimit import hit, refund
from assessments.services import (
    finalize_attempt,
    publish_quiz,
    save_response,
    start_attempt,
    void_attempt,
)


class R3Fixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="R3 Org", slug="r3-org")
        self.instructor = User.objects.create_user("r3ins", password="pass-phrase-12")
        self.student = User.objects.create_user("r3stu", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        OrganizationMembership.objects.create(
            user=self.student,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )


@override_settings(MFA_REQUIRED=True)
class MFASecretEpochTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="R3 MFA", slug="r3-mfa")
        self.admin = User.objects.create_user("r3admin", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.secret = new_totp_secret()
        self.admin.mfa_secret = self.secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])

    def test_secret_rotation_changes_epoch(self):
        before = mfa_privilege_epoch(self.admin)
        self.admin.mfa_secret = new_totp_secret()
        self.admin.save(update_fields=["mfa_secret"])
        self.assertNotEqual(before, mfa_privilege_epoch(self.admin))

    def test_stale_epoch_after_rotation_forces_reverify(self):
        client = Client()
        client.force_login(self.admin)
        # Session satisfied against the current secret.
        code = pyotp.TOTP(self.secret).now()
        self.assertEqual(
            client.post("/accounts/mfa/verify/", {"code": code}).status_code, 302
        )
        # Rotate the secret out-of-band (other device / admin path).
        self.admin.mfa_secret = new_totp_secret()
        self.admin.save(update_fields=["mfa_secret"])
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])


@override_settings(MFA_REQUIRED=True)
class StaffMFATests(TestCase):
    def test_staff_without_org_admin_requires_mfa(self):
        staff = User.objects.create_user("staffer", password="pass-phrase-12", is_staff=True)
        client = Client()
        client.force_login(staff)
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/setup/", response["Location"])


class VoidPolicyEditTests(R3Fixture):
    def test_policy_edit_cannot_reopen_keyed_retake(self):
        quiz = Quiz.objects.create(
            organization=self.org,
            title="Imm",
            created_by=self.instructor,
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="I", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="y", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="n", is_correct=False, sort_order=2)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Imm A",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.get()
        correct = item.choice_snapshots.get(is_correct=True)
        save_response(attempt, self.student, item.id, [correct.id], 0)
        self.assertTrue(Response.objects.filter(attempt=attempt, is_locked=True).exists())
        finalize_attempt(attempt, reason="submitted")
        void_attempt(attempt, self.instructor, reason="rescore")

        assignment.feedback_policy = Quiz.FeedbackPolicy.AT_END
        assignment.save(update_fields=["feedback_policy"])
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assignment)


class RefundRaceTests(TestCase):
    def test_refund_uses_decr_not_stale_delete(self):
        cache.clear()
        scope, identity, window = "login", "ip:1.2.3.4|u", 300
        self.assertTrue(hit(scope, identity, limit=10, window_seconds=window))
        # Simulate a concurrent hit landing after a would-be get.
        self.assertTrue(hit(scope, identity, limit=10, window_seconds=window))
        refund(scope, identity, window)
        # One unit should remain — a stale delete would have wiped both.
        from assessments.ratelimit import _bucket_key

        remaining = cache.get(_bucket_key(scope, identity, window))
        self.assertEqual(remaining, 1)


class PoolMinItemsTests(R3Fixture):
    def test_empty_extras_do_not_create_pool(self):
        client = Client()
        client.force_login(self.instructor)
        before = ItemPool.objects.count()
        response = client.post(
            "/instructor/pools/new/",
            {
                "name": "EmptyPool",
                "required_question_count": "2",
                "blueprint": "{}",
                "itempoolitem_set-TOTAL_FORMS": "3",
                "itempoolitem_set-INITIAL_FORMS": "0",
                "itempoolitem_set-MIN_NUM_FORMS": "0",
                "itempoolitem_set-MAX_NUM_FORMS": "1000",
                "itempoolitem_set-0-item": "",
                "itempoolitem_set-0-weight": "1",
                "itempoolitem_set-1-item": "",
                "itempoolitem_set-1-weight": "1",
                "itempoolitem_set-2-item": "",
                "itempoolitem_set-2-weight": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPool.objects.count(), before)
        self.assertFalse(ItemPool.objects.filter(name="EmptyPool").exists())


class PasswordResetThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("resetme", "reset@example.com", "pass-phrase-12")

    def test_password_reset_is_rate_limited(self):
        from django.conf import settings

        client = Client()
        for _ in range(settings.LOGIN_RATE_LIMIT):
            client.post(
                "/accounts/password_reset/",
                {"email": "reset@example.com"},
            )
        response = client.post(
            "/accounts/password_reset/",
            {"email": "reset@example.com"},
        )
        self.assertEqual(response.status_code, 429)


class MFAAdminAuditTests(TestCase):
    def test_reset_logs_even_without_memberships(self):
        operator = User.objects.create_user(
            "platop",
            password="pass-phrase-12",
            is_platform_operator=True,
            is_staff=True,
        )
        operator.mfa_enabled = True
        operator.mfa_secret = new_totp_secret()
        operator.save(update_fields=["mfa_enabled", "mfa_secret"])
        actor = User.objects.create_superuser("super", "s@ex.com", "pass-phrase-12")
        request = RequestFactory().get("/admin/")
        request.user = actor

        class _Admin:
            def message_user(self, *args, **kwargs):
                return None

        before = LogEntry.objects.count()
        reset_user_mfa(_Admin(), request, User.objects.filter(pk=operator.pk))
        self.assertGreater(LogEntry.objects.count(), before)
        operator.refresh_from_db()
        self.assertFalse(operator.mfa_enabled)
        self.assertEqual(operator.mfa_secret, "")

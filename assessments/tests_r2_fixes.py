"""Regressions for Round 2 audit findings R2-01–R2-12."""

from __future__ import annotations

from datetime import timedelta

import pyotp
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.mfa import mfa_privilege_epoch, new_totp_secret
from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Attempt,
    Choice,
    DailyChallenge,
    IntegrityEvent,
    ItemPool,
    Question,
    Quiz,
    QuizAssignment,
)
from assessments.services import (
    finalize_attempt,
    publish_quiz,
    start_attempt,
    start_practice_attempt,
    void_attempt,
)
from assessments.tasks import finalize_expired_attempts


class R2Fixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="R2 Org", slug="r2-org")
        self.instructor = User.objects.create_user("r2ins", password="pass-phrase-12")
        self.student = User.objects.create_user("r2stu", password="pass-phrase-12")
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
        quiz = Quiz.objects.create(
            organization=self.org,
            title="Q",
            created_by=self.instructor,
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="P", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="yes", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="no", is_correct=False, sort_order=2)
        self.version = publish_quiz(quiz, self.instructor)


@override_settings(MFA_REQUIRED=True)
class MFATakeoverTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="MFA2", slug="mfa2")
        self.admin = User.objects.create_user("mfa2admin", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.secret = new_totp_secret()
        self.admin.mfa_secret = self.secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])

    def test_setup_blocked_while_enrolled_and_unverified(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get("/accounts/mfa/setup/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])

    def test_setup_cannot_overwrite_secret_before_verify(self):
        client = Client()
        client.force_login(self.admin)
        new_secret = new_totp_secret()
        # Even a crafted POST to setup must bounce to verify.
        response = client.post(
            "/accounts/mfa/setup/",
            {"code": pyotp.TOTP(new_secret).now()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.mfa_secret, self.secret)

    def test_role_promotion_invalidates_mfa_ok(self):
        student = User.objects.create_user("promoted", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=student,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        client = Client()
        client.force_login(student)
        # Simulate a satisfied non-privileged session.
        session = client.session
        session["mfa_ok_user_id"] = student.pk
        session["mfa_ok_epoch"] = mfa_privilege_epoch(student)
        session.save()

        OrganizationMembership.objects.create(
            user=student,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/", response["Location"])

    def test_mfa_verify_is_rate_limited(self):
        client = Client()
        client.force_login(self.admin)
        for _ in range(5):
            client.post("/accounts/mfa/verify/", {"code": "000000"})
        response = client.post("/accounts/mfa/verify/", {"code": "000000"})
        self.assertEqual(response.status_code, 429)


class SweepAttributionTests(R2Fixture):
    def test_sweep_emits_sweep_finalized(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Sweep me",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(finalize_expired_attempts(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.finalization_reason, "sweep")
        self.assertTrue(
            IntegrityEvent.objects.filter(
                attempt=attempt, kind=IntegrityEvent.Kind.SWEEP_FINALIZED
            ).exists()
        )


class PracticeLegacyRowTests(R2Fixture):
    def test_existing_practice_assignment_is_upgraded_to_unlimited(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org,
            quiz_version=self.version,
            release_date=timezone.now().date() - timedelta(days=3),
            title="Old",
            is_archived=True,
        )
        QuizAssignment.objects.create(
            organization=self.org,
            title=f"Practice: {challenge.title} ({challenge.release_date})",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(days=1),
            attempt_limit=1,
            created_by=self.instructor,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
        )
        first = start_practice_attempt(self.student, challenge)
        finalize_attempt(first, reason="submitted")
        second = start_practice_attempt(self.student, challenge)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.assignment.attempt_limit, 0)


class VoidImmediateRetakeTests(R2Fixture):
    def test_void_after_immediate_still_consumes_limit(self):
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
        finalize_attempt(attempt, reason="submitted")
        void_attempt(attempt, self.instructor, reason="key leak risk")
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assignment)


class PoolAtomicTests(R2Fixture):
    def test_invalid_formset_does_not_create_pool(self):
        client = Client()
        client.force_login(self.instructor)
        before = ItemPool.objects.count()
        response = client.post(
            "/instructor/pools/new/",
            {
                "name": "Broken",
                "required_question_count": "1",
                "blueprint": "{}",
                "itempoolitem_set-TOTAL_FORMS": "1",
                "itempoolitem_set-INITIAL_FORMS": "0",
                "itempoolitem_set-MIN_NUM_FORMS": "0",
                "itempoolitem_set-MAX_NUM_FORMS": "1000",
                # Non-existent bank item — formset invalid, pool must not persist.
                "itempoolitem_set-0-item": "999999",
                "itempoolitem_set-0-weight": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPool.objects.count(), before)
        self.assertFalse(ItemPool.objects.filter(name="Broken").exists())


class LoginRefundTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("refundu", password="pass-phrase-12")

    def test_successful_login_refunds_budget(self):
        from django.conf import settings

        client = Client()
        # Spend almost the whole window on successes; refund keeps us under limit.
        for _ in range(settings.LOGIN_RATE_LIMIT):
            response = client.post(
                "/accounts/login/",
                {"username": "refundu", "password": "pass-phrase-12"},
            )
            self.assertEqual(response.status_code, 302)
            client.logout()
        response = client.post(
            "/accounts/login/",
            {"username": "refundu", "password": "pass-phrase-12"},
        )
        self.assertEqual(response.status_code, 302)

"""Regressions for Round 4 audit findings R4-01–R4-06."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pyotp
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.mfa import mfa_privilege_epoch, new_totp_secret
from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Choice,
    ItemPool,
    Question,
    QuestionBank,
    QuestionBankItem,
    Quiz,
    QuizAssignment,
)
from assessments.services import finalize_attempt, publish_quiz, start_attempt, void_attempt


class R4Fixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="R4 Org", slug="r4-org")
        self.instructor = User.objects.create_user("r4ins", password="pass-phrase-12")
        self.student = User.objects.create_user("r4stu", password="pass-phrase-12")
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


class AtEndVoidDisclosureTests(R4Fixture):
    def test_void_after_at_end_key_release_blocks_retake(self):
        quiz = Quiz.objects.create(
            organization=self.org,
            title="AtEnd",
            created_by=self.instructor,
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="Q", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="y", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="n", is_correct=False, sort_order=2)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="AtEnd A",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        attempt.refresh_from_db()
        self.assertTrue(attempt.answer_key_disclosed)
        void_attempt(attempt, self.instructor, reason="rescore")
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assignment)


class AdminLoginThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user(
            "admstaff", password="pass-phrase-12", is_staff=True
        )

    def test_admin_login_shares_accounts_login_budget(self):
        client = Client()
        for _ in range(settings.LOGIN_RATE_LIMIT):
            client.post(
                "/accounts/login/",
                {"username": "admstaff", "password": "wrong-password-xx"},
            )
        response = client.post(
            "/admin/login/",
            {"username": "admstaff", "password": "pass-phrase-12", "next": "/admin/"},
        )
        self.assertEqual(response.status_code, 429)


@override_settings(MFA_REQUIRED=False)
class MFAEnforcementFlipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="R4 MFA", slug="r4-mfa")
        self.admin = User.objects.create_user("r4admin", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.secret = new_totp_secret()
        self.admin.mfa_secret = self.secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])

    def test_login_does_not_stamp_mfa_ok_when_enforcement_off(self):
        from accounts.mfa import SESSION_MFA_USER

        client = Client()
        response = client.post(
            "/accounts/login/",
            {"username": "r4admin", "password": "pass-phrase-12"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(client.session.get(SESSION_MFA_USER))

    @override_settings(MFA_REQUIRED=True)
    def test_enforcement_bit_changes_epoch(self):
        with override_settings(MFA_REQUIRED=False):
            off = mfa_privilege_epoch(self.admin)
        with override_settings(MFA_REQUIRED=True):
            on = mfa_privilege_epoch(self.admin)
        self.assertNotEqual(off, on)

    def test_enabling_mfa_required_forces_challenge(self):
        client = Client()
        client.post(
            "/accounts/login/",
            {"username": "r4admin", "password": "pass-phrase-12"},
        )
        # Session is authenticated under MFA_REQUIRED=False (class override).
        with override_settings(MFA_REQUIRED=True):
            response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/verify/", response["Location"])


class BlueprintStrataFormsetTests(R4Fixture):
    def test_blueprint_strata_reject_wrong_difficulties(self):
        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        items = []
        for i in range(3):
            item = QuestionBankItem.objects.create(
                organization=self.org,
                bank=bank,
                prompt=f"Easy {i}",
                question_type=QuestionBankItem.QuestionType.SINGLE,
                difficulty=QuestionBankItem.Difficulty.EASY,
                review_status="approved",
                created_by=self.instructor,
            )
            items.append(item)

        client = Client()
        client.force_login(self.instructor)
        before = ItemPool.objects.count()
        response = client.post(
            "/instructor/pools/new/",
            {
                "name": "StrataBad",
                "required_question_count": "3",
                "blueprint": '{"easy": 1, "medium": 1, "hard": 1}',
                "itempoolitem_set-TOTAL_FORMS": "3",
                "itempoolitem_set-INITIAL_FORMS": "0",
                "itempoolitem_set-MIN_NUM_FORMS": "0",
                "itempoolitem_set-MAX_NUM_FORMS": "1000",
                "itempoolitem_set-0-item": str(items[0].id),
                "itempoolitem_set-0-weight": "1",
                "itempoolitem_set-1-item": str(items[1].id),
                "itempoolitem_set-1-weight": "1",
                "itempoolitem_set-2-item": str(items[2].id),
                "itempoolitem_set-2-weight": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPool.objects.count(), before)
        self.assertContains(response, "Blueprint strata are short")


class PasswordResetTemplateTests(TestCase):
    def test_branded_reset_email_templates_exist(self):
        root = Path(settings.BASE_DIR) / "templates" / "registration"
        self.assertTrue((root / "password_reset_email.html").is_file())
        self.assertTrue((root / "password_reset_subject.txt").is_file())
        subject = (root / "password_reset_subject.txt").read_text(encoding="utf-8")
        self.assertIn("QuizOps", subject)


class MFATotpStillWorksTests(TestCase):
    @override_settings(MFA_REQUIRED=True)
    def test_successful_totp_still_satisfies_session(self):
        cache.clear()
        org = Organization.objects.create(name="R4 TOTP", slug="r4-totp")
        admin = User.objects.create_user("r4totp", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=admin,
            organization=org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        secret = new_totp_secret()
        admin.mfa_secret = secret
        admin.mfa_enabled = True
        admin.save(update_fields=["mfa_secret", "mfa_enabled"])

        client = Client()
        client.post("/accounts/login/", {"username": "r4totp", "password": "pass-phrase-12"})
        code = pyotp.TOTP(secret).now()
        response = client.post("/accounts/mfa/verify/", {"code": code})
        self.assertEqual(response.status_code, 302)
        home = client.get("/")
        self.assertEqual(home.status_code, 200)

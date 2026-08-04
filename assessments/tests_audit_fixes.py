"""Regressions for the post-remediation audit findings F01–F17."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from accounts.mfa import new_totp_secret, verify_totp
from accounts.models import Organization, OrganizationMembership, User
from accounts.privacy import anonymize_user
from assessments.models import (
    Choice,
    DailyChallenge,
    IntegrityEvent,
    ItemPool,
    ItemPoolItem,
    JobFailure,
    Question,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
    QuizAssignment,
    ResponseEvent,
    TimingAccommodation,
)
from assessments.services import (
    effective_attempt_limit,
    finalize_attempt,
    publish_quiz,
    start_attempt,
    start_practice_attempt,
    void_attempt,
)
from assessments.tasks import _students_for, apply_retention_policies


class AuditFixFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Org", slug="audit-org")
        self.instructor = User.objects.create_user("ainstructor", password="pass-phrase-12")
        self.student = User.objects.create_user("astudent", password="pass-phrase-12")
        self.explicit = User.objects.create_user("aexplicit", password="pass-phrase-12")
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
        OrganizationMembership.objects.create(
            user=self.explicit,
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


class PracticeUnlimitedTests(AuditFixFixture):
    def test_practice_allows_multiple_attempts(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org,
            quiz_version=self.version,
            release_date=timezone.now().date() - timedelta(days=2),
            title="Past",
            is_archived=True,
        )
        first = start_practice_attempt(self.student, challenge)
        finalize_attempt(first, reason="submitted")
        second = start_practice_attempt(self.student, challenge)
        self.assertNotEqual(first.id, second.id)
        self.assertIsNone(effective_attempt_limit(second.assignment))


class VoidDoesNotConsumeLimitTests(AuditFixFixture):
    def test_voided_attempt_frees_the_slot(self):
        # Limit 2 so AT_END withholds the key on attempt 1; void must free the
        # slot (F04). Limit 1 would disclose the key and keep the void (R4-01).
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Twice",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=2,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        attempt.refresh_from_db()
        self.assertFalse(attempt.answer_key_disclosed)
        void_attempt(attempt, self.instructor, reason="bad item")
        again = start_attempt(self.student, assignment)
        self.assertEqual(again.assignment_id, assignment.id)


class ExplicitStudentNotifyTests(AuditFixFixture):
    def test_students_for_includes_explicit_assignees(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Named",
            quiz_version=self.version,
            opens_at=timezone.now(),
            due_at=timezone.now() + timedelta(hours=12),
            created_by=self.instructor,
        )
        assignment.explicit_students.add(self.explicit)
        recipients = set(_students_for(assignment).values_list("id", flat=True))
        self.assertIn(self.explicit.id, recipients)


class SweepAttributionTests(AuditFixFixture):
    def test_interactive_expiry_is_not_sweep_finalized(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Timed",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="expired")
        self.assertTrue(
            IntegrityEvent.objects.filter(
                attempt=attempt, kind=IntegrityEvent.Kind.EXPIRED_FINALIZED
            ).exists()
        )
        self.assertFalse(
            IntegrityEvent.objects.filter(
                attempt=attempt, kind=IntegrityEvent.Kind.SWEEP_FINALIZED
            ).exists()
        )


class ErasureRedactsAccommodationTests(AuditFixFixture):
    def test_erasure_clears_accommodation_reason(self):
        TimingAccommodation.objects.create(
            organization=self.org,
            student=self.student,
            multiplier=Decimal("1.50"),
            reason="medical documentation on file",
        )
        anonymize_user(self.student, actor=self.instructor)
        self.student.refresh_from_db()
        self.assertTrue(self.student.username.startswith("erased-"))
        self.assertEqual(
            TimingAccommodation.objects.get(student=self.student).reason,
            "",
        )


class RetentionJobTests(AuditFixFixture):
    def test_retention_clears_old_network_fields_and_purges_rows(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="R",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.get()
        event = ResponseEvent.objects.create(
            attempt=attempt,
            item_snapshot=item,
            selected_choice_ids=[],
            revision_before=0,
            revision_after=1,
            ip_address="203.0.113.9",
            user_agent="old-agent",
        )
        ResponseEvent.objects.filter(pk=event.pk).update(
            created_at=timezone.now() - timedelta(days=800)
        )
        integrity = IntegrityEvent.objects.create(
            organization=self.org,
            attempt=attempt,
            kind=IntegrityEvent.Kind.REVISION_CONFLICT,
            detail={},
        )
        IntegrityEvent.objects.filter(pk=integrity.pk).update(
            created_at=timezone.now() - timedelta(days=800)
        )
        job = JobFailure.objects.create(task_name="x", error="boom")
        JobFailure.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(days=120)
        )

        result = apply_retention_policies()
        event.refresh_from_db()
        self.assertIsNone(event.ip_address)
        self.assertEqual(event.user_agent, "")
        self.assertFalse(IntegrityEvent.objects.filter(pk=integrity.pk).exists())
        self.assertFalse(JobFailure.objects.filter(pk=job.pk).exists())
        self.assertGreaterEqual(result["response_network_cleared"], 1)


class ImmediateRetakeValidationTests(AuditFixFixture):
    def test_fixed_immediate_with_retakes_rejected(self):
        assignment = QuizAssignment(
            organization=self.org,
            title="Leak",
            form_mode=QuizAssignment.FormMode.FIXED,
            quiz_version=self.version,
            attempt_limit=2,
            feedback_policy=Quiz.FeedbackPolicy.IMMEDIATE,
            created_by=self.instructor,
            opens_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean()


class PoolAuthoringTests(AuditFixFixture):
    def test_instructor_can_create_a_pool(self):
        bank = QuestionBank.objects.create(
            organization=self.org, name="Bank", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org,
            bank=bank,
            prompt="Bank Q",
            question_type="single",
            created_by=self.instructor,
            review_status="approved",
        )
        QuestionChoice.objects.create(item=item, text="a", is_correct=True, sort_order=1)
        QuestionChoice.objects.create(item=item, text="b", is_correct=False, sort_order=2)

        client = Client()
        client.force_login(self.instructor)
        response = client.post(
            "/instructor/pools/new/",
            {
                "name": "Pool A",
                "required_question_count": "1",
                "blueprint": "{}",
                "itempoolitem_set-TOTAL_FORMS": "1",
                "itempoolitem_set-INITIAL_FORMS": "0",
                "itempoolitem_set-MIN_NUM_FORMS": "0",
                "itempoolitem_set-MAX_NUM_FORMS": "1000",
                "itempoolitem_set-0-item": str(item.id),
                "itempoolitem_set-0-weight": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        pool = ItemPool.objects.get(name="Pool A")
        self.assertEqual(ItemPoolItem.objects.filter(pool=pool).count(), 1)


@override_settings(MFA_REQUIRED=True)
class MFAEnforcementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="MFA Org", slug="mfa-org")
        self.admin = User.objects.create_user("mfaadmin", password="pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin,
            organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )

    def test_org_admin_is_redirected_to_mfa_setup(self):
        client = Client()
        client.force_login(self.admin)
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/mfa/setup/", response["Location"])

    def test_valid_totp_satisfies_mfa(self):
        secret = new_totp_secret()
        self.admin.mfa_secret = secret
        self.admin.mfa_enabled = True
        self.admin.save(update_fields=["mfa_secret", "mfa_enabled"])
        client = Client()
        client.force_login(self.admin)
        import pyotp

        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(secret, code))
        response = client.post("/accounts/mfa/verify/", {"code": code})
        self.assertEqual(response.status_code, 302)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)

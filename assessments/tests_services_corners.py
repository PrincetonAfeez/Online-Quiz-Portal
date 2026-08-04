"""Remaining service-layer branch coverage for policy and visibility helpers."""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Attempt,
    Choice,
    Question,
    Quiz,
    QuizAssignment,
    ResultRelease,
)
from assessments.services import (
    attempts_counting_toward_limit,
    effective_attempt_limit,
    finalize_attempt,
    mark_answer_key_disclosed,
    publish_quiz,
    result_visibility,
    start_attempt,
    void_attempt,
)

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tests-services-corners",
    }
}


@override_settings(CACHES=LOCMEM_CACHES)
class ServicesCornerFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Corner Org", slug="corner-org")
        self.instructor = User.objects.create_user("cins", password="pass-phrase-12")
        self.student = User.objects.create_user("cstu", password="pass-phrase-12")
        self.staff = User.objects.create_user(
            "cstaff", password="pass-phrase-12", is_staff=True
        )
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
            title="Corner Quiz",
            created_by=self.instructor,
            attempt_limit=2,
            feedback_policy=Quiz.FeedbackPolicy.AT_END,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="Q", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="yes", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="no", is_correct=False, sort_order=2)
        self.version = publish_quiz(quiz, self.instructor)

    def _assignment(self, **kwargs):
        defaults = {
            "organization": self.org,
            "title": "Corner Assignment",
            "quiz_version": self.version,
            "opens_at": timezone.now() - timedelta(minutes=1),
            "attempt_limit": 2,
            "feedback_policy": Quiz.FeedbackPolicy.AT_END,
            "created_by": self.instructor,
        }
        defaults.update(kwargs)
        return QuizAssignment.objects.create(**defaults)


class MarkAnswerKeyDisclosedTests(ServicesCornerFixture):
    def test_mark_answer_key_disclosed_is_idempotent(self):
        assignment = self._assignment(attempt_limit=1)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        attempt.refresh_from_db()
        self.assertTrue(attempt.answer_key_disclosed)
        mark_answer_key_disclosed(attempt)
        mark_answer_key_disclosed(attempt)
        self.assertTrue(
            Attempt.objects.filter(pk=attempt.pk, answer_key_disclosed=True).exists()
        )


class EffectiveAttemptLimitCornerTests(ServicesCornerFixture):
    def test_zero_on_assignment_means_unlimited_practice(self):
        assignment = self._assignment(attempt_limit=0)
        self.assertIsNone(effective_attempt_limit(assignment))

    def test_zero_on_quiz_version_means_unlimited_when_inherited(self):
        quiz = Quiz.objects.create(
            organization=self.org,
            title="Practice Quiz",
            created_by=self.instructor,
            attempt_limit=0,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="P", question_type="single", points=1, sort_order=1
        )
        Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
        version = publish_quiz(quiz, self.instructor)
        assignment = self._assignment(
            title="Inherited Practice",
            quiz_version=version,
            attempt_limit=None,
        )
        self.assertIsNone(effective_attempt_limit(assignment))


class AttemptsCountingDisclosedTests(ServicesCornerFixture):
    def test_voided_attempt_still_counts_after_key_disclosed(self):
        assignment = self._assignment(attempt_limit=2)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        attempt.refresh_from_db()
        mark_answer_key_disclosed(attempt)
        void_attempt(attempt, self.instructor, reason="rescore")
        self.assertEqual(
            attempts_counting_toward_limit(self.student, assignment),
            1,
        )

    def test_voided_attempt_does_not_count_without_disclosure(self):
        assignment = self._assignment(attempt_limit=2)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        attempt.refresh_from_db()
        self.assertFalse(attempt.answer_key_disclosed)
        void_attempt(attempt, self.instructor, reason="rescore")
        self.assertEqual(
            attempts_counting_toward_limit(self.student, assignment),
            0,
        )


class ResultVisibilityCornerTests(ServicesCornerFixture):
    def test_voided_attempt_hides_everything_from_student(self):
        assignment = self._assignment(attempt_limit=1)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        attempt = void_attempt(attempt, self.instructor, reason="bad item")
        vis = result_visibility(attempt, self.student)
        self.assertEqual(vis.reason, "voided")
        self.assertFalse(vis.show_score)
        self.assertFalse(vis.show_answer_key)

    def test_instructor_sees_score_and_key(self):
        assignment = self._assignment(attempt_limit=1)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        attempt.refresh_from_db()
        vis = result_visibility(attempt, self.instructor)
        self.assertEqual(vis.reason, "staff")
        self.assertTrue(vis.show_score)
        self.assertTrue(vis.show_answer_key)

    def test_manual_policy_withholds_until_release_open(self):
        assignment = self._assignment(
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.MANUAL,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        attempt.refresh_from_db()
        vis = result_visibility(attempt, self.student)
        self.assertEqual(vis.reason, "manual_release_withheld")
        self.assertFalse(vis.show_score)
        self.assertFalse(vis.show_answer_key)

    def test_retake_available_shows_score_not_key(self):
        assignment = self._assignment(attempt_limit=2)
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        attempt.refresh_from_db()
        vis = result_visibility(attempt, self.student)
        self.assertEqual(vis.reason, "retake_available")
        self.assertTrue(vis.show_score)
        self.assertFalse(vis.show_answer_key)
        self.assertEqual(vis.extras["attempts_used"], 1)
        self.assertEqual(vis.extras["attempt_limit"], 2)

    def test_in_progress_attempt_is_hidden(self):
        assignment = self._assignment(attempt_limit=1)
        attempt = start_attempt(self.student, assignment)
        vis = result_visibility(attempt, self.student)
        self.assertEqual(vis.reason, "in_progress")
        self.assertFalse(vis.show_score)
        self.assertFalse(vis.show_answer_key)

    def test_manual_release_open_shows_results(self):
        assignment = self._assignment(
            attempt_limit=1,
            feedback_policy=Quiz.FeedbackPolicy.MANUAL,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        attempt.refresh_from_db()
        ResultRelease.objects.create(
            organization=self.org, assignment=assignment, is_open=True
        )
        attempt = Attempt.objects.select_related("assignment__result_release").get(
            pk=attempt.pk
        )
        vis = result_visibility(attempt, self.student)
        self.assertEqual(vis.reason, "released")
        self.assertTrue(vis.show_score)
        self.assertTrue(vis.show_answer_key)

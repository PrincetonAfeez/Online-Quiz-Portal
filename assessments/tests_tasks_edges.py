"""Edge-case coverage for assessments.tasks helpers and periodic jobs."""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Choice,
    Cohort,
    Course,
    Enrollment,
    ExportArtifact,
    IntegrityEvent,
    JobFailure,
    Notification,
    Question,
    Quiz,
    QuizAssignment,
    ResponseEvent,
)
from assessments.services import finalize_attempt, publish_quiz, start_attempt
from assessments.tasks import (
    RESPONSE_NETWORK_RETENTION,
    _students_for,
    apply_retention_policies,
    cleanup_expired_exports,
    send_due_soon_notifications,
)

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tests-tasks-edges",
    }
}


def build_version(org, instructor):
    quiz = Quiz.objects.create(
        organization=org, title="Q", created_by=instructor, time_limit_minutes=30
    )
    question = Question.objects.create(
        quiz=quiz, prompt="P", question_type="single", points=1
    )
    Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
    Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
    return publish_quiz(quiz, instructor)


@override_settings(CACHES=LOCMEM_CACHES)
class TasksEdgeFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Tasks Org", slug="tasks-org")
        self.instructor = User.objects.create_user("tins", password="pass-phrase-12")
        self.student = User.objects.create_user("tstu", password="pass-phrase-12")
        self.other = User.objects.create_user("tother", password="pass-phrase-12")
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
            user=self.other,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        self.course = Course.objects.create(organization=self.org, name="Course T")
        self.cohort = Cohort.objects.create(
            organization=self.org, course=self.course, name="Section A"
        )
        Enrollment.objects.create(
            organization=self.org,
            student=self.student,
            course=self.course,
            cohort=self.cohort,
            is_active=True,
        )
        self.version = build_version(self.org, self.instructor)


class CleanupExportOSErrorTests(TasksEdgeFixture):
    def test_oserror_on_remove_skips_delete_and_counts_zero(self):
        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        artifact = ExportArtifact.objects.create(
            organization=self.org,
            requested_by=self.instructor,
            name="stuck",
            file_path=path,
            expires_at=timezone.now() - timedelta(days=1),
        )
        with patch("os.remove", side_effect=OSError("permission denied")):
            self.assertEqual(cleanup_expired_exports(), 0)
        self.assertTrue(ExportArtifact.objects.filter(pk=artifact.pk).exists())
        self.assertTrue(os.path.exists(path))
        os.remove(path)

    def test_missing_file_still_deletes_the_row(self):
        ExportArtifact.objects.create(
            organization=self.org,
            requested_by=self.instructor,
            name="gone",
            file_path="/nonexistent/export.csv",
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(cleanup_expired_exports(), 1)
        self.assertEqual(ExportArtifact.objects.count(), 0)


class RetentionPolicyEdgeTests(TasksEdgeFixture):
    def test_already_cleared_network_fields_are_not_recounted(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Retention",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.get()
        cleared = ResponseEvent.objects.create(
            attempt=attempt,
            item_snapshot=item,
            selected_choice_ids=[],
            revision_before=0,
            revision_after=1,
            ip_address=None,
            user_agent="",
        )
        ResponseEvent.objects.filter(pk=cleared.pk).update(
            created_at=timezone.now() - RESPONSE_NETWORK_RETENTION - timedelta(days=1)
        )
        stale = ResponseEvent.objects.create(
            attempt=attempt,
            item_snapshot=item,
            selected_choice_ids=[],
            revision_before=1,
            revision_after=2,
            ip_address="198.51.100.4",
            user_agent="legacy-browser",
        )
        ResponseEvent.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - RESPONSE_NETWORK_RETENTION - timedelta(days=1)
        )

        result = apply_retention_policies()

        stale.refresh_from_db()
        self.assertIsNone(stale.ip_address)
        self.assertEqual(stale.user_agent, "")
        self.assertEqual(result["response_network_cleared"], 1)

    def test_recent_network_fields_are_left_alone(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Fresh",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.get()
        ResponseEvent.objects.create(
            attempt=attempt,
            item_snapshot=item,
            selected_choice_ids=[],
            revision_before=0,
            revision_after=1,
            ip_address="203.0.113.1",
            user_agent="current",
        )
        result = apply_retention_policies()
        self.assertEqual(result["response_network_cleared"], 0)
        self.assertEqual(
            ResponseEvent.objects.filter(ip_address="203.0.113.1").count(),
            1,
        )


class StudentsForBranchTests(TasksEdgeFixture):
    def _due_assignment(self, **kwargs):
        defaults = {
            "organization": self.org,
            "title": "Due",
            "quiz_version": self.version,
            "opens_at": timezone.now() - timedelta(minutes=1),
            "due_at": timezone.now() + timedelta(hours=6),
            "created_by": self.instructor,
        }
        defaults.update(kwargs)
        return QuizAssignment.objects.create(**defaults)

    def test_students_for_scopes_to_cohort_enrollment(self):
        assignment = self._due_assignment(cohort=self.cohort)
        recipients = set(_students_for(assignment).values_list("username", flat=True))
        self.assertEqual(recipients, {"tstu"})

    def test_students_for_scopes_to_course_enrollment(self):
        assignment = self._due_assignment(course=self.course)
        recipients = set(_students_for(assignment).values_list("username", flat=True))
        self.assertEqual(recipients, {"tstu"})

    def test_students_for_org_wide_includes_all_students(self):
        assignment = self._due_assignment()
        recipients = set(_students_for(assignment).values_list("username", flat=True))
        self.assertEqual(recipients, {"tstu", "tother"})

    def test_students_for_merges_explicit_assignees(self):
        assignment = self._due_assignment(cohort=self.cohort)
        outsider = User.objects.create_user("texplicit", password="pass-phrase-12")
        assignment.explicit_students.add(outsider)
        recipients = set(_students_for(assignment).values_list("username", flat=True))
        self.assertIn("texplicit", recipients)
        self.assertIn("tstu", recipients)

    def test_send_due_soon_skips_students_with_scored_attempts(self):
        assignment = self._due_assignment()
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, reason="submitted")
        before = Notification.objects.filter(user=self.student).count()
        sent = send_due_soon_notifications(hours_ahead=24)
        self.assertEqual(sent, 1)
        self.assertEqual(
            Notification.objects.filter(user=self.student).count(),
            before,
        )
        self.assertTrue(
            Notification.objects.filter(user=self.other, kind=Notification.Kind.DUE_SOON).exists()
        )


class SingleFlightTaskSkipTests(TasksEdgeFixture):
    def test_cleanup_skips_when_lock_is_held(self):
        cache.add("joblock:cleanup_expired_exports", "1", 600)
        ExportArtifact.objects.create(
            organization=self.org,
            requested_by=self.instructor,
            name="locked",
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(cleanup_expired_exports(), 0)
        self.assertEqual(ExportArtifact.objects.count(), 1)

    def test_retention_skips_when_lock_is_held(self):
        cache.add("joblock:apply_retention_policies", "1", 600)
        JobFailure.objects.create(task_name="old", error="stale")
        JobFailure.objects.filter(error="stale").update(
            created_at=timezone.now() - timedelta(days=120)
        )
        result = apply_retention_policies()
        self.assertEqual(result, 0)
        self.assertTrue(JobFailure.objects.filter(error="stale").exists())

    def test_due_soon_skips_when_lock_is_held(self):
        cache.add("joblock:send_due_soon_notifications", "1", 600)
        QuizAssignment.objects.create(
            organization=self.org,
            title="Soon",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            due_at=timezone.now() + timedelta(hours=2),
            is_active=True,
            created_by=self.instructor,
        )
        self.assertEqual(send_due_soon_notifications(), 0)
        self.assertEqual(Notification.objects.count(), 0)

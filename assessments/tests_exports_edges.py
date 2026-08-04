"""Export edge cases — CSV safety and smoke tests for export helpers."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.exports import _csv_safe, gradebook_csv, item_analysis_csv
from assessments.models import (
    Choice,
    Question,
    Quiz,
    QuizAssignment,
)
from assessments.services import finalize_attempt, publish_quiz, save_response, start_attempt


class ExportEdgeFixture(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Org", slug="org")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.instructor,
            organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        quiz = Quiz.objects.create(
            organization=self.org, title="Q", created_by=self.instructor
        )
        q = Question.objects.create(
            quiz=quiz, prompt="Prompt?", question_type="single", points=1
        )
        Choice.objects.create(question=q, text="a", is_correct=True, sort_order=1)
        Choice.objects.create(question=q, text="b", is_correct=False, sort_order=2)
        self.version = publish_quiz(quiz, self.instructor)

    def _assignment(self):
        return QuizAssignment.objects.create(
            organization=self.org,
            title="Export",
            quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )


class CsvSafeTests(TestCase):
    def test_none_becomes_empty_string(self):
        self.assertEqual(_csv_safe(None), "")

    def test_formula_injection_prefixes_are_neutralised(self):
        for prefix in ("=", "+", "-", "@"):
            result = _csv_safe(f"{prefix}SUM(A1)")
            self.assertTrue(result.startswith("'"))
            self.assertIn(prefix, result)

    def test_tab_and_cr_prefixes_are_neutralised(self):
        for char in ("\t", "\r"):
            result = _csv_safe(f"{char}payload")
            self.assertTrue(result.startswith("'"))

    def test_safe_values_pass_through(self):
        self.assertEqual(_csv_safe("Alice"), "Alice")
        self.assertEqual(_csv_safe(42), "42")
        self.assertEqual(_csv_safe("0.75"), "0.75")


class ExportSmokeTests(ExportEdgeFixture):
    def test_gradebook_csv_header_and_empty_body(self):
        text = gradebook_csv(self._assignment())
        lines = text.strip().split("\n")
        self.assertEqual(
            lines[0],
            "username,student,attempts,best_score,best_percent,passed,late,latest_status,finalized_at",
        )
        self.assertEqual(len(lines), 1)

    def test_item_analysis_csv_header_with_no_attempts(self):
        text = item_analysis_csv(self._assignment())
        lines = [line for line in text.strip().split("\n") if line]
        self.assertEqual(lines[0], "question,responses,correct,difficulty,discrimination,flags")
        self.assertEqual(len(lines), 1)

    def test_gradebook_csv_escapes_injected_student_name(self):
        evil = User.objects.create_user("evil", "evil@t.dev", "pass-phrase-12")
        evil.first_name = "=1+1"
        evil.save()
        OrganizationMembership.objects.create(
            user=evil,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        assignment = self._assignment()
        attempt = start_attempt(evil, assignment)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(is_correct=True)
        save_response(attempt, evil, item.id, [correct.id], revision=0)
        finalize_attempt(attempt, actor=evil)

        csv_text = gradebook_csv(assignment)
        self.assertIn("'=1+1", csv_text)
        self.assertNotIn(",=1+1", csv_text)

    def test_item_analysis_csv_includes_item_row(self):
        student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=student,
            organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        assignment = self._assignment()
        attempt = start_attempt(student, assignment)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(is_correct=True)
        save_response(attempt, student, item.id, [correct.id], revision=0)
        finalize_attempt(attempt, actor=student)

        text = item_analysis_csv(assignment)
        self.assertIn("Prompt?", text)
        self.assertIn("question,responses", text.split("\n")[0])

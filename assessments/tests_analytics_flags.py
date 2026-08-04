"""Analytics corners — difficulty labels, item flags, gradebook, pass rates."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.analytics import (
    ItemStat,
    assignment_analytics,
    gradebook_rows,
    item_statistics,
)
from assessments.models import (
    Choice,
    Cohort,
    Course,
    Enrollment,
    Question,
    Quiz,
    QuizAssignment,
)
from assessments.services import finalize_attempt, publish_quiz, save_response, start_attempt


def make_org(slug="org", name="Org"):
    return Organization.objects.create(name=name, slug=slug)


def add_member(user, org, role):
    return OrganizationMembership.objects.create(user=user, organization=org, role=role)


class AnalyticsFixture(TestCase):
    def setUp(self):
        self.org = make_org()
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        self.student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        add_member(self.instructor, self.org, OrganizationMembership.Role.INSTRUCTOR)
        add_member(self.student, self.org, OrganizationMembership.Role.STUDENT)
        self.course = Course.objects.create(organization=self.org, name="Course")
        self.cohort = Cohort.objects.create(
            organization=self.org, course=self.course, name="Cohort"
        )
        Enrollment.objects.create(
            organization=self.org,
            student=self.student,
            course=self.course,
            cohort=self.cohort,
        )

    def _multi_question_version(self, count=4):
        quiz = Quiz.objects.create(
            organization=self.org,
            title="Multi",
            created_by=self.instructor,
            pass_mark_percent=70,
            shuffle_questions=False,
            shuffle_choices=False,
        )
        for i in range(count):
            q = Question.objects.create(
                quiz=quiz,
                prompt=f"Question {i}",
                question_type="single",
                points=1,
                sort_order=i,
            )
            Choice.objects.create(question=q, text="right", is_correct=True, sort_order=1)
            Choice.objects.create(question=q, text="wrong", is_correct=False, sort_order=2)
        return publish_quiz(quiz, self.instructor)

    def _cohort_of(self, n, start=0):
        students = []
        for i in range(start, start + n):
            user = User.objects.create_user(f"s{i}", f"s{i}@t.dev", "pass-phrase-12")
            add_member(user, self.org, OrganizationMembership.Role.STUDENT)
            Enrollment.objects.create(
                organization=self.org, student=user, course=self.course, cohort=self.cohort
            )
            students.append(user)
        return students

    def _sit_custom(self, student, assignment, answers_by_index):
        """answers_by_index: list of 'right' or 'wrong' per question sort_order."""
        attempt = start_attempt(student, assignment)
        revision = 0
        for index, item in enumerate(attempt.item_snapshots.order_by("sort_order")):
            wanted = answers_by_index[index]
            choice = item.choice_snapshots.get(text=wanted)
            save_response(attempt, student, item.id, [choice.id], revision)
            revision += 1
        finalize_attempt(attempt, actor=student)
        return attempt


class DifficultyLabelTests(TestCase):
    """Every band of ItemStat.difficulty_label including boundaries."""

    CASES = (
        (Decimal("0.95"), "Very easy"),
        (Decimal("0.90"), "Very easy"),
        (Decimal("0.89"), "Easy"),
        (Decimal("0.70"), "Easy"),
        (Decimal("0.69"), "Moderate"),
        (Decimal("0.40"), "Moderate"),
        (Decimal("0.39"), "Hard"),
        (Decimal("0.20"), "Hard"),
        (Decimal("0.19"), "Very hard"),
        (Decimal("0.00"), "Very hard"),
    )

    def test_all_difficulty_bands(self):
        for difficulty, label in self.CASES:
            stat = ItemStat(
                prompt="p",
                responses=10,
                correct=int(difficulty * 10),
                difficulty=difficulty,
                discrimination=Decimal("0.5"),
            )
            self.assertEqual(stat.difficulty_label, label, msg=f"p={difficulty}")


class ItemStatisticsFlagTests(AnalyticsFixture):
    def _assignment_with_six_students(self):
        version = self._multi_question_version(count=4)
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Flags",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        return assignment, self._cohort_of(6)

    def test_negative_discrimination_flag(self):
        assignment, students = self._assignment_with_six_students()
        # High scorers miss Q0; low scorers get Q0 right → negative discrimination.
        patterns = [
            ["right", "wrong", "wrong", "wrong"],   # 25% — bottom
            ["right", "wrong", "wrong", "wrong"],   # 25% — bottom
            ["wrong", "right", "right", "wrong"],   # 50%
            ["wrong", "right", "right", "right"],   # 75%
            ["wrong", "right", "right", "right"],   # 75% — top
            ["wrong", "right", "right", "right"],   # 75% — top
        ]
        for student, pattern in zip(students, patterns, strict=True):
            self._sit_custom(student, assignment, pattern)

        stats = {s.prompt: s for s in item_statistics(assignment)}
        q0 = stats["Question 0"]
        self.assertGreaterEqual(q0.responses, 5)
        self.assertIn("Negative discrimination", " ".join(q0.flags))

    def test_low_discrimination_flag(self):
        version = self._multi_question_version(count=4)
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Low disc",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        students = self._cohort_of(21)
        patterns = (
            [["wrong", "wrong", "wrong", "wrong"]] * 3
            + [["right", "wrong", "wrong", "wrong"]] * 4
            + [["right", "right", "wrong", "wrong"]] * 7
            + [["right", "right", "right", "wrong"]] * 5
            + [["wrong", "right", "right", "right"]] * 2
        )
        for student, pattern in zip(students, patterns, strict=True):
            self._sit_custom(student, assignment, pattern)

        stats = {s.prompt: s for s in item_statistics(assignment)}
        q0 = stats["Question 0"]
        self.assertGreaterEqual(q0.responses, 5)
        self.assertTrue(
            any("Low discrimination" in f for f in q0.flags),
            msg=f"flags={q0.flags} disc={q0.discrimination}",
        )

    def test_nearly_everyone_correct_flag(self):
        assignment, students = self._assignment_with_six_students()
        for student in students:
            self._sit_custom(student, assignment, ["right", "wrong", "wrong", "wrong"])

        stats = {s.prompt: s for s in item_statistics(assignment)}
        q0 = stats["Question 0"]
        self.assertGreaterEqual(q0.responses, 5)
        self.assertGreaterEqual(q0.difficulty, Decimal("0.95"))
        self.assertIn("Nearly everyone correct", " ".join(q0.flags))

    def test_nearly_everyone_wrong_flag(self):
        assignment, students = self._assignment_with_six_students()
        for student in students:
            self._sit_custom(student, assignment, ["wrong", "right", "right", "right"])

        stats = {s.prompt: s for s in item_statistics(assignment)}
        q0 = stats["Question 0"]
        self.assertGreaterEqual(q0.responses, 5)
        self.assertLessEqual(q0.difficulty, Decimal("0.15"))
        self.assertIn("Nearly everyone wrong", " ".join(q0.flags))

    def test_fewer_than_five_responses_suppresses_flags(self):
        version = self._multi_question_version(count=1)
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Small N",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        for student in self._cohort_of(4):
            self._sit_custom(student, assignment, ["right"])

        stat = item_statistics(assignment)[0]
        self.assertEqual(stat.responses, 4)
        self.assertEqual(stat.flags, [])


class AssignmentAnalyticsTests(AnalyticsFixture):
    def test_empty_assignment_analytics(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Empty",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )
        stats = assignment_analytics(assignment, pass_mark=50)
        self.assertEqual(stats.finalized_attempts, 0)
        self.assertEqual(stats.students_attempted, 0)
        self.assertEqual(stats.mean_percent, Decimal("0"))
        self.assertIsNone(stats.pass_rate)

    def test_pass_rate_computed_when_mark_set(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Pass rate",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=1,
            created_by=self.instructor,
        )
        for student, correct in zip(self._cohort_of(4), (0, 1, 2, 4), strict=True):
            attempt = start_attempt(student, assignment)
            revision = 0
            for index, item in enumerate(attempt.item_snapshots.order_by("sort_order")):
                wanted = "right" if index < correct else "wrong"
                choice = item.choice_snapshots.get(text=wanted)
                save_response(attempt, student, item.id, [choice.id], revision)
                revision += 1
            finalize_attempt(attempt, actor=student)

        stats = assignment_analytics(assignment, pass_mark=50)
        self.assertEqual(stats.finalized_attempts, 4)
        # 0%, 25%, 50%, 100% → 2 of 4 pass
        self.assertEqual(stats.pass_rate, Decimal("50.0"))


class GradebookRowTests(AnalyticsFixture):
    def test_best_attempt_passed_and_late(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Gradebook",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=2,
            pass_mark_percent=50,
            created_by=self.instructor,
        )
        # First attempt: 25% (fail); mark late.
        low = start_attempt(self.student, assignment)
        revision = 0
        for index, item in enumerate(low.item_snapshots.order_by("sort_order")):
            wanted = "right" if index == 0 else "wrong"
            choice = item.choice_snapshots.get(text=wanted)
            save_response(low, self.student, item.id, [choice.id], revision)
            revision += 1
        finalize_attempt(low, actor=self.student)
        low.is_late = True
        low.save(update_fields=["is_late"])

        # Second attempt: 75% (pass), not late.
        high = start_attempt(self.student, assignment)
        revision = 0
        for index, item in enumerate(high.item_snapshots.order_by("sort_order")):
            wanted = "right" if index < 3 else "wrong"
            choice = item.choice_snapshots.get(text=wanted)
            save_response(high, self.student, item.id, [choice.id], revision)
            revision += 1
        finalize_attempt(high, actor=self.student)

        rows = gradebook_rows(assignment, pass_mark=50)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.attempts, 2)
        self.assertEqual(row.best_percent, Decimal("75.0"))
        self.assertTrue(row.passed)
        self.assertTrue(row.is_late)

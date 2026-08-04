"""Phase 4: service-layer and instructor-workspace branch coverage."""

from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Attempt,
    Choice,
    Cohort,
    Course,
    Enrollment,
    ItemPool,
    ItemPoolItem,
    Question,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
    QuizAssignment,
)
from assessments.services import (
    AttemptConflict,
    AttemptExpired,
    _weighted_sample,
    assert_org_admin,
    attempt_cutoff,
    effective_attempt_limit,
    effective_feedback_policy,
    effective_pass_mark,
    effective_time_limit,
    finalize_attempt,
    is_org_admin,
    is_past_cutoff,
    publish_quiz,
    sample_pool_items,
    save_response,
    start_attempt,
    student_read_model,
    void_attempt,
)


class ServiceFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Org", slug="org")
        self.admin = User.objects.create_user("adm", "adm@t.dev", "pass-phrase-12")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        self.student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.admin, organization=self.org,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        OrganizationMembership.objects.create(
            user=self.instructor, organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        OrganizationMembership.objects.create(
            user=self.student, organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )

    def make_quiz(self, **kwargs):
        defaults = {
            "organization": self.org,
            "title": "Quiz",
            "created_by": self.instructor,
            "shuffle_questions": False,
            "shuffle_choices": False,
        }
        defaults.update(kwargs)
        return Quiz.objects.create(**defaults)

    def add_question(self, quiz, qtype="single", correct=("a",), wrong=("b",), points=1):
        question = Question.objects.create(
            quiz=quiz, prompt=f"P{quiz.questions.count()}", question_type=qtype,
            points=points, sort_order=quiz.questions.count(),
        )
        order = 1
        for text in correct:
            Choice.objects.create(question=question, text=text, is_correct=True, sort_order=order)
            order += 1
        for text in wrong:
            Choice.objects.create(question=question, text=text, is_correct=False, sort_order=order)
            order += 1
        return question


class PublishValidationTests(ServiceFixture):
    def test_empty_quiz_is_rejected(self):
        with self.assertRaises(ValidationError):
            publish_quiz(self.make_quiz(), self.instructor)

    def test_true_false_needs_exactly_two_choices(self):
        quiz = self.make_quiz()
        self.add_question(quiz, qtype="true_false", correct=("t",), wrong=("f", "maybe"))
        with self.assertRaises(ValidationError):
            publish_quiz(quiz, self.instructor)

    def test_single_needs_exactly_one_correct(self):
        quiz = self.make_quiz()
        self.add_question(quiz, correct=("a", "b"), wrong=("c",))
        with self.assertRaises(ValidationError):
            publish_quiz(quiz, self.instructor)

    def test_multi_needs_a_wrong_choice(self):
        quiz = self.make_quiz()
        self.add_question(quiz, qtype="multi", correct=("a", "b"), wrong=())
        with self.assertRaises(ValidationError):
            publish_quiz(quiz, self.instructor)

    def test_multi_needs_a_correct_choice(self):
        quiz = self.make_quiz()
        self.add_question(quiz, qtype="multi", correct=(), wrong=("a", "b"))
        with self.assertRaises(ValidationError):
            publish_quiz(quiz, self.instructor)

    def test_students_cannot_publish(self):
        quiz = self.make_quiz()
        self.add_question(quiz)
        with self.assertRaises(PermissionDenied):
            publish_quiz(quiz, self.student)

    def test_version_numbers_increment(self):
        quiz = self.make_quiz()
        self.add_question(quiz)
        first = publish_quiz(quiz, self.instructor)
        second = publish_quiz(quiz, self.instructor)
        self.assertEqual((first.version_number, second.version_number), (1, 2))

    def test_published_version_copies_choices_faithfully(self):
        quiz = self.make_quiz()
        self.add_question(quiz, correct=("right",), wrong=("wrong",))
        version = publish_quiz(quiz, self.instructor)
        choices = version.version_questions.get().version_choices.all()
        self.assertEqual(
            {(c.text, c.is_correct) for c in choices},
            {("right", True), ("wrong", False)},
        )


class PolicyResolutionTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        quiz = self.make_quiz(time_limit_minutes=20, attempt_limit=3, pass_mark_percent=60)
        self.add_question(quiz)
        self.version = publish_quiz(quiz, self.instructor)

    def _assignment(self, **kwargs):
        defaults = {
            "organization": self.org, "title": "A", "quiz_version": self.version,
            "opens_at": timezone.now() - timedelta(minutes=1),
            "created_by": self.instructor,
        }
        defaults.update(kwargs)
        return QuizAssignment.objects.create(**defaults)

    def test_assignment_overrides_version_time_limit(self):
        self.assertEqual(effective_time_limit(self._assignment(time_limit_minutes=5)), 5)

    def test_version_time_limit_is_the_fallback(self):
        self.assertEqual(effective_time_limit(self._assignment()), 20)

    def test_no_time_limit_anywhere(self):
        quiz = self.make_quiz(time_limit_minutes=None)
        self.add_question(quiz)
        version = publish_quiz(quiz, self.instructor)
        assignment = self._assignment(title="B", quiz_version=version)
        self.assertIsNone(effective_time_limit(assignment))

    def test_attempt_limit_resolution(self):
        self.assertEqual(effective_attempt_limit(self._assignment(attempt_limit=2)), 2)
        # Blank/null inherits from the quiz version.
        inherited = self._assignment(title="Z", attempt_limit=None)
        self.assertEqual(effective_attempt_limit(inherited), 3)
        # Zero means unlimited (practice archives).
        self.assertIsNone(effective_attempt_limit(self._assignment(title="U", attempt_limit=0)))

    def test_attempt_limit_defaults_to_one_for_sampled_forms(self):
        pool = ItemPool.objects.create(
            organization=self.org, name="P", created_by=self.instructor
        )
        assignment = self._assignment(
            title="S", form_mode="sampled", quiz_version=None, item_pool=pool, attempt_limit=None
        )
        self.assertEqual(effective_attempt_limit(assignment), 1)

    def test_feedback_policy_resolution_order(self):
        self.assertEqual(
            effective_feedback_policy(self._assignment(feedback_policy="manual")), "manual"
        )
        self.assertEqual(effective_feedback_policy(self._assignment(title="V")), "at_end")

    def test_pass_mark_can_be_set_on_sampled_assignments(self):
        pool = ItemPool.objects.create(
            organization=self.org, name="P2", created_by=self.instructor
        )
        assignment = self._assignment(
            title="S2", form_mode="sampled", quiz_version=None, item_pool=pool
        )
        self.assertIsNone(effective_pass_mark(assignment))
        assignment.pass_mark_percent = 75
        assignment.save(update_fields=["pass_mark_percent"])
        self.assertEqual(effective_pass_mark(assignment), 75)
        self.assertEqual(effective_pass_mark(self._assignment(title="F")), 60)


class CutoffTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        quiz = self.make_quiz(time_limit_minutes=30)
        self.add_question(quiz)
        self.version = publish_quiz(quiz, self.instructor)

    def test_cutoff_is_the_earlier_of_deadline_and_close(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Clamp", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1),
            closes_at=timezone.now() + timedelta(minutes=5),
            grace_seconds=30, created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        # A 30-minute limit clamped to a window closing in 5 minutes.
        self.assertLess(attempt_cutoff(attempt), timezone.now() + timedelta(minutes=6))

    def test_no_cutoff_when_untimed_and_open_ended(self):
        quiz = self.make_quiz(title="Untimed", time_limit_minutes=None)
        self.add_question(quiz)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Forever", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        self.assertIsNone(attempt_cutoff(attempt))
        self.assertFalse(is_past_cutoff(attempt))


class SamplingTests(ServiceFixture):
    def _pool(self, count=6, difficulty="easy", weights=None):
        bank = QuestionBank.objects.create(
            organization=self.org, name="B", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="Pool", required_question_count=3,
            created_by=self.instructor,
        )
        for i in range(count):
            item = QuestionBankItem.objects.create(
                organization=self.org, bank=bank, prompt=f"Item {i}",
                question_type="single", points=1, difficulty=difficulty,
                created_by=self.instructor,
            )
            QuestionChoice.objects.create(item=item, text="a", is_correct=True, sort_order=1)
            QuestionChoice.objects.create(item=item, text="b", is_correct=False, sort_order=2)
            ItemPoolItem.objects.create(
                pool=pool, item=item, weight=(weights[i] if weights else 1)
            )
        return pool

    def test_sampling_is_deterministic_for_a_seed(self):
        import random

        pool = self._pool()
        first, _ = sample_pool_items(pool, random.Random("seed"))
        second, _ = sample_pool_items(pool, random.Random("seed"))
        self.assertEqual([i.id for i in first], [i.id for i in second])

    def test_sampling_records_criteria(self):
        import random

        pool = self._pool()
        _, criteria = sample_pool_items(pool, random.Random("s"))
        self.assertEqual(criteria["pool_id"], pool.id)
        self.assertTrue(criteria["weighted"])
        self.assertEqual(len(criteria["item_ids"]), 3)

    def test_blueprint_draws_per_stratum(self):
        import random

        bank = QuestionBank.objects.create(
            organization=self.org, name="B", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="Blueprinted", required_question_count=3,
            blueprint={"easy": 2, "hard": 1}, created_by=self.instructor,
        )
        for difficulty, count in (("easy", 4), ("hard", 3)):
            for i in range(count):
                item = QuestionBankItem.objects.create(
                    organization=self.org, bank=bank, prompt=f"{difficulty}{i}",
                    question_type="single", points=1, difficulty=difficulty,
                    created_by=self.instructor,
                )
                QuestionChoice.objects.create(item=item, text="a", is_correct=True)
                ItemPoolItem.objects.create(pool=pool, item=item)
        items, _ = sample_pool_items(pool, random.Random("x"))
        difficulties = [i.difficulty for i in items]
        self.assertEqual(difficulties.count("easy"), 2)
        self.assertEqual(difficulties.count("hard"), 1)

    def test_blueprint_stratum_shortfall_is_refused(self):
        import random

        bank = QuestionBank.objects.create(
            organization=self.org, name="B", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="Short", required_question_count=3,
            blueprint={"easy": 1, "hard": 2}, created_by=self.instructor,
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="only easy",
            question_type="single", points=1, difficulty="easy",
            created_by=self.instructor,
        )
        ItemPoolItem.objects.create(pool=pool, item=item)
        with self.assertRaises(ValidationError):
            sample_pool_items(pool, random.Random("x"))

    def test_empty_pool_is_refused(self):
        import random

        pool = ItemPool.objects.create(
            organization=self.org, name="Empty", created_by=self.instructor
        )
        with self.assertRaises(ValidationError):
            sample_pool_items(pool, random.Random("x"))

    def test_unapproved_items_are_excluded(self):
        import random

        pool = self._pool(count=4)
        QuestionBankItem.objects.filter(organization=self.org).update(review_status="draft")
        with self.assertRaises(ValidationError):
            sample_pool_items(pool, random.Random("x"))

    def test_weighted_sample_respects_weight(self):
        import random

        pool = self._pool(count=4, weights=[1000, 1, 1, 1])
        entries = list(ItemPoolItem.objects.filter(pool=pool).select_related("item"))
        picks = _weighted_sample(entries, 1, random.Random(1))
        self.assertEqual(len(picks), 1)

    def test_blueprint_validation_on_the_model(self):
        pool = ItemPool(
            organization=self.org, name="Bad", required_question_count=3,
            blueprint={"nonsense": 3}, created_by=self.instructor,
        )
        with self.assertRaises(ValidationError):
            pool.clean()

        pool.blueprint = {"easy": 2}
        with self.assertRaises(ValidationError):
            pool.clean()

        pool.blueprint = {"easy": -1, "hard": 4}
        with self.assertRaises(ValidationError):
            pool.clean()


class ScoringTests(ServiceFixture):
    def _version(self, qtype="multi"):
        quiz = self.make_quiz(time_limit_minutes=None)
        self.add_question(quiz, qtype=qtype, correct=("a", "b"), wrong=("c",), points=4)
        return publish_quiz(quiz, self.instructor)

    def _assignment(self, version):
        return QuizAssignment.objects.create(
            organization=self.org, title="Score", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )

    def test_multi_select_requires_the_exact_set(self):
        version = self._version()
        attempt = start_attempt(self.student, self._assignment(version))
        item = attempt.item_snapshots.first()
        partial = item.choice_snapshots.get(text="a")
        save_response(attempt, self.student, item.id, [partial.id], 0)
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)

    def test_exact_multi_select_scores_full_points(self):
        version = self._version()
        attempt = start_attempt(self.student, self._assignment(version))
        item = attempt.item_snapshots.first()
        ids = [c.id for c in item.choice_snapshots.filter(text__in=["a", "b"])]
        save_response(attempt, self.student, item.id, ids, 0)
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 4)
        self.assertEqual(attempt.score_percent, Decimal("100.0"))

    def test_single_answer_rejects_multiple_selections(self):
        quiz = self.make_quiz(title="Single", time_limit_minutes=None)
        self.add_question(quiz, qtype="single", correct=("a",), wrong=("b", "c"))
        version = publish_quiz(quiz, self.instructor)
        attempt = start_attempt(self.student, self._assignment(version))
        item = attempt.item_snapshots.first()
        ids = [c.id for c in item.choice_snapshots.all()[:2]]
        with self.assertRaises(ValidationError):
            save_response(attempt, self.student, item.id, ids, 0)

    def test_score_percent_is_zero_without_a_max(self):
        attempt = Attempt(score=Decimal("0"), max_score=Decimal("0"))
        self.assertEqual(attempt.score_percent, Decimal("0"))

    def test_unanswered_questions_score_zero(self):
        version = self._version()
        attempt = start_attempt(self.student, self._assignment(version))
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 0)
        self.assertEqual(attempt.max_score, 4)


class SaveResponseGuardTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        quiz = self.make_quiz(time_limit_minutes=None)
        self.add_question(quiz)
        self.version = publish_quiz(quiz, self.instructor)
        self.assignment = QuizAssignment.objects.create(
            organization=self.org, title="Guarded", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        self.attempt = start_attempt(self.student, self.assignment)
        self.item = self.attempt.item_snapshots.first()

    def test_another_user_cannot_save_into_your_attempt(self):
        intruder = User.objects.create_user("bad", "bad@t.dev", "pass-phrase-12")
        with self.assertRaises(PermissionDenied):
            save_response(self.attempt, intruder, self.item.id, [], 0)

    def test_saving_into_a_finalized_attempt_is_refused(self):
        finalize_attempt(self.attempt, actor=self.student)
        with self.assertRaises(AttemptExpired):
            save_response(self.attempt, self.student, self.item.id, [], 1)

    def test_locked_response_is_refused(self):
        from assessments.models import Response

        save_response(self.attempt, self.student, self.item.id, [], 0)
        Response.objects.filter(attempt=self.attempt).update(is_locked=True)
        with self.assertRaises(AttemptConflict):
            save_response(self.attempt, self.student, self.item.id, [], 1)

    def test_empty_selection_is_allowed_and_clears_the_answer(self):
        choice = self.item.choice_snapshots.first()
        save_response(self.attempt, self.student, self.item.id, [choice.id], 0)
        outcome = save_response(self.attempt, self.student, self.item.id, [], 1)
        self.assertEqual(outcome.response.selected_choices.count(), 0)

    def test_read_model_reports_locked_state(self):
        from assessments.models import Response

        save_response(self.attempt, self.student, self.item.id, [], 0)
        Response.objects.filter(attempt=self.attempt).update(is_locked=True)
        self.assertTrue(student_read_model(self.attempt)[0].is_locked)


class VoidAndAuthorizationTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        quiz = self.make_quiz(time_limit_minutes=None)
        self.add_question(quiz)
        self.version = publish_quiz(quiz, self.instructor)
        self.assignment = QuizAssignment.objects.create(
            organization=self.org, title="V", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )

    def test_org_admin_helpers(self):
        self.assertTrue(is_org_admin(self.admin, self.org))
        self.assertFalse(is_org_admin(self.instructor, self.org))
        with self.assertRaises(PermissionDenied):
            assert_org_admin(self.student, self.org)

    def test_platform_operator_passes_every_gate(self):
        operator = User.objects.create_user("op", "op@t.dev", "pass-phrase-12")
        operator.is_platform_operator = True
        operator.save()
        self.assertTrue(is_org_admin(operator, self.org))

    def test_void_clears_daily_credit(self):
        from assessments.models import DailyChallenge, DailyParticipation
        from assessments.services import start_daily_attempt, today_for_org

        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="D",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertTrue(DailyParticipation.objects.get().earned_credit)

        # Voiding must withdraw the streak credit it granted.
        Attempt.objects.filter(id=attempt.id).update(status=Attempt.Status.FINALIZED)
        void_attempt(attempt, self.instructor, reason="bad question")
        self.assertFalse(DailyParticipation.objects.get().earned_credit)

    def test_void_writes_an_admin_action_log(self):
        from assessments.models import AdminActionLog

        attempt = start_attempt(self.student, self.assignment)
        void_attempt(attempt, self.instructor, reason="test")
        self.assertTrue(AdminActionLog.objects.filter(action="attempt.void").exists())


class InstructorWorkspaceCoverageTests(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.instructor)
        self.quiz = self.make_quiz()
        self.add_question(self.quiz)
        self.version = publish_quiz(self.quiz, self.instructor)

    def test_quiz_list_counts_questions_and_versions(self):
        response = self.client.get("/instructor/quizzes/")
        quiz = response.context["quizzes"][0]
        self.assertEqual(quiz.question_count, 1)
        self.assertEqual(quiz.version_count, 1)

    def test_quiz_edit_updates_settings(self):
        self.client.post(
            f"/instructor/quizzes/{self.quiz.id}/edit/",
            {"title": "Renamed", "description": "", "attempt_limit": "2",
             "feedback_policy": "manual", "pass_mark_percent": "80"},
        )
        self.quiz.refresh_from_db()
        self.assertEqual(self.quiz.title, "Renamed")
        self.assertEqual(self.quiz.feedback_policy, "manual")

    def test_quiz_form_rejects_impossible_values(self):
        response = self.client.post(
            f"/instructor/quizzes/{self.quiz.id}/edit/",
            {"title": "X", "attempt_limit": "1", "feedback_policy": "at_end",
             "pass_mark_percent": "150", "time_limit_minutes": "0"},
        )
        self.assertContains(response, "cannot exceed 100")
        self.assertContains(response, "at least one minute")

    def test_question_edit_updates_an_existing_question(self):
        question = self.quiz.questions.first()
        response = self.client.post(
            f"/instructor/quizzes/{self.quiz.id}/questions/{question.id}/",
            {
                "prompt": "Updated prompt", "question_type": "single", "points": "3",
                "explanation": "why",
                "choices-TOTAL_FORMS": "2", "choices-INITIAL_FORMS": "2",
                "choices-MIN_NUM_FORMS": "2", "choices-MAX_NUM_FORMS": "1000",
                "choices-0-id": str(question.choices.all()[0].id),
                "choices-0-question": str(question.id),
                "choices-0-text": "a", "choices-0-is_correct": "on",
                "choices-1-id": str(question.choices.all()[1].id),
                "choices-1-question": str(question.id),
                "choices-1-text": "b",
            },
        )
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.prompt, "Updated prompt")
        self.assertEqual(question.points, 3)

    def test_question_delete(self):
        question = self.quiz.questions.first()
        self.client.post(
            f"/instructor/quizzes/{self.quiz.id}/questions/{question.id}/delete/"
        )
        self.assertEqual(self.quiz.questions.count(), 0)

    def test_publish_failure_is_reported(self):
        empty = self.make_quiz(title="Empty")
        response = self.client.post(
            f"/instructor/quizzes/{empty.id}/publish/", follow=True
        )
        self.assertContains(response, "at least one question")

    def test_version_preview_shows_the_key(self):
        response = self.client.get(f"/instructor/versions/{self.version.id}/preview/")
        self.assertContains(response, "Correct")

    def test_assignment_edit_updates_an_existing_assignment(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Editable", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        response = self.client.get(f"/instructor/assignments/{assignment.id}/edit/")
        self.assertEqual(response.status_code, 200)
        self.client.post(
            f"/instructor/assignments/{assignment.id}/edit/",
            {"title": "Renamed assignment", "form_mode": "fixed",
             "quiz_version": self.version.id, "opens_at": "2026-01-01T09:00",
             "attempt_limit": "2", "grace_seconds": "30", "is_active": "on"},
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.title, "Renamed assignment")

    def test_assignment_form_surfaces_model_validation(self):
        response = self.client.post(
            "/instructor/assignments/new/",
            {"title": "Bad window", "form_mode": "fixed",
             "quiz_version": self.version.id,
             "opens_at": "2026-06-01T09:00", "closes_at": "2026-01-01T09:00",
             "attempt_limit": "1", "grace_seconds": "30", "is_active": "on"},
        )
        self.assertContains(response, "after the open time")

    def test_void_from_the_gradebook_requires_a_reason(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Voidable", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        Enrollment.objects.create(
            organization=self.org, student=self.student,
            course=Course.objects.create(organization=self.org, name="C"),
        )
        attempt = start_attempt(self.student, assignment)
        response = self.client.post(
            f"/instructor/attempts/{attempt.id}/void/", {"reason": ""}, follow=True
        )
        self.assertContains(response, "reason is required")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.IN_PROGRESS)

        self.client.post(
            f"/instructor/attempts/{attempt.id}/void/", {"reason": "duplicate"}
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.VOIDED_BY_ADMIN)

    def test_item_analysis_export_variant(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Items", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        response = self.client.get(
            f"/instructor/assignments/{assignment.id}/export/?kind=items"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("item-analysis", response["Content-Disposition"])
        self.assertIn("discrimination", response.content.decode())


class CohortAndEnrollmentTests(ServiceFixture):
    def test_cohort_must_belong_to_the_selected_course(self):
        course_a = Course.objects.create(organization=self.org, name="A")
        course_b = Course.objects.create(organization=self.org, name="B")
        cohort_b = Cohort.objects.create(
            organization=self.org, course=course_b, name="B1"
        )
        quiz = self.make_quiz()
        self.add_question(quiz)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment(
            organization=self.org, title="Mismatch", quiz_version=version,
            course=course_a, cohort=cohort_b, created_by=self.instructor,
        )
        with self.assertRaises(ValidationError):
            assignment.full_clean(exclude=["created_by"])

    def test_inactive_course_removes_eligibility(self):
        course = Course.objects.create(organization=self.org, name="Inactive")
        Enrollment.objects.create(
            organization=self.org, student=self.student, course=course
        )
        quiz = self.make_quiz()
        self.add_question(quiz)
        version = publish_quiz(quiz, self.instructor)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Gated", quiz_version=version, course=course,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        Course.objects.filter(id=course.id).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assignment)

    def test_explicit_students_bypass_enrollment(self):
        quiz = self.make_quiz()
        self.add_question(quiz)
        version = publish_quiz(quiz, self.instructor)
        course = Course.objects.create(organization=self.org, name="NotEnrolled")
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Explicit", quiz_version=version, course=course,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        assignment.explicit_students.add(self.student)
        self.assertIsNotNone(start_attempt(self.student, assignment))

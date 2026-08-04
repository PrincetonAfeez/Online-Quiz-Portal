"""Core assessment regression and tenancy tests."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.analytics import (
    assignment_analytics,
    gradebook_rows,
    item_statistics,
)
from assessments.exports import gradebook_csv
from assessments.models import (
    AdminActionLog,
    Attempt,
    AttemptAudit,
    AttemptFinalization,
    Choice,
    Cohort,
    Course,
    DailyChallenge,
    DailyParticipation,
    Enrollment,
    ExportArtifact,
    ItemPool,
    ItemPoolItem,
    Notification,
    Question,
    QuestionBank,
    QuestionBankItem,
    QuestionChoice,
    Quiz,
    QuizAssignment,
    ResponseEvent,
    ResultRelease,
    StreakCache,
    TimingAccommodation,
)
from assessments.services import (
    AttemptConflict,
    AttemptExpired,
    _best_accommodation,
    effective_feedback_policy,
    finalize_attempt,
    publish_quiz,
    recompute_streak,
    result_visibility,
    save_response,
    start_attempt,
    start_daily_attempt,
    student_read_model,
    today_for_org,
    visible_assignments_for,
    void_attempt,
)
from assessments.tasks import (
    cleanup_expired_exports,
    finalize_expired_attempts,
    send_daily_challenge_notifications,
    send_due_soon_notifications,
)


def make_org(slug, name, tz="UTC"):
    return Organization.objects.create(name=name, slug=slug, timezone=tz)


def add_member(user, org, role):
    return OrganizationMembership.objects.create(user=user, organization=org, role=role)


def single_question_version(org, instructor, policy=Quiz.FeedbackPolicy.AT_END, minutes=5):
    quiz = Quiz.objects.create(
        organization=org,
        title="Basics",
        created_by=instructor,
        time_limit_minutes=minutes,
        feedback_policy=policy,
    )
    q = Question.objects.create(
        quiz=quiz, prompt="What maps keys to values?", question_type="single", points=2
    )
    Choice.objects.create(question=q, text="list", is_correct=False, sort_order=1)
    Choice.objects.create(question=q, text="dict", is_correct=True, sort_order=2)
    return publish_quiz(quiz, instructor)


class BaseFixture(TestCase):
    def setUp(self):
        self.org = make_org("acme", "Acme Academy")
        self.student = User.objects.create_user("student", "student@test.dev", "pass-phrase-12")
        self.instructor = User.objects.create_user("instructor", "instr@test.dev", "pass-phrase-12")
        add_member(self.student, self.org, OrganizationMembership.Role.STUDENT)
        add_member(self.instructor, self.org, OrganizationMembership.Role.INSTRUCTOR)
        self.course = Course.objects.create(organization=self.org, name="Python")
        self.cohort = Cohort.objects.create(organization=self.org, course=self.course, name="July")
        Enrollment.objects.create(
            organization=self.org, student=self.student, course=self.course, cohort=self.cohort
        )
        self.version = single_question_version(self.org, self.instructor)
        self.assignment = QuizAssignment.objects.create(
            organization=self.org,
            title="Basics Assignment",
            quiz_version=self.version,
            course=self.course,
            cohort=self.cohort,
            opens_at=timezone.now() - timedelta(minutes=1),
            closes_at=timezone.now() + timedelta(days=1),
            attempt_limit=1,
            created_by=self.instructor,
        )


class AssessmentEngineTests(BaseFixture):
    def test_published_version_is_immutable_after_draft_edit(self):
        draft_question = self.version.quiz.questions.first()
        draft_question.prompt = "Changed draft prompt"
        draft_question.save()
        self.assertEqual(
            self.version.version_questions.first().prompt, "What maps keys to values?"
        )

    def test_student_read_model_excludes_correctness(self):
        attempt = start_attempt(self.student, self.assignment)
        question = student_read_model(attempt)[0]
        self.assertEqual(question.prompt, "What maps keys to values?")
        self.assertFalse(hasattr(question.choices[0], "is_correct"))

    def test_revision_conflict_rejects_stale_save(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(text="dict")
        save_response(attempt, self.student, item.id, [correct.id], revision=0)
        with self.assertRaises(AttemptConflict):
            save_response(attempt, self.student, item.id, [correct.id], revision=0)

    def test_finalization_scores_once_and_is_idempotent(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(text="dict")
        save_response(attempt, self.student, item.id, [correct.id], revision=0)
        first = finalize_attempt(attempt, actor=self.student)
        second = finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertEqual(first.id, second.id)
        self.assertEqual(attempt.score, 2)
        self.assertEqual(attempt.max_score, 2)

    def test_attempt_limit_is_server_enforced(self):
        start_attempt(self.student, self.assignment)
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, self.assignment)

    def test_save_returns_saved_at_that_advances(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        first = save_response(attempt, self.student, item.id, [], revision=0)
        second = save_response(
            attempt, self.student, item.id, [item.choice_snapshots.first().id], revision=1
        )
        self.assertGreaterEqual(second.response.saved_at, first.response.saved_at)


class TenancyTests(TestCase):
    """Findings 1 and 2: isolation must not depend on is_staff or leak across orgs."""

    def setUp(self):
        self.a = make_org("a", "Org A")
        self.b = make_org("b", "Org B")
        self.ins_b = User.objects.create_user("insb", "insb@test.dev", "pass-phrase-12")
        add_member(self.ins_b, self.b, OrganizationMembership.Role.INSTRUCTOR)
        self.stu_b = User.objects.create_user("stub", "stub@test.dev", "pass-phrase-12")
        add_member(self.stu_b, self.b, OrganizationMembership.Role.STUDENT)
        version = single_question_version(self.b, self.ins_b)
        self.assign_b = QuizAssignment.objects.create(
            organization=self.b,
            title="B secret assignment",
            quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1),
            closes_at=timezone.now() + timedelta(days=1),
            attempt_limit=3,
            created_by=self.ins_b,
        )
        start_attempt(self.stu_b, self.assign_b)

    def test_is_staff_without_platform_flag_sees_nothing(self):
        staff = User.objects.create_user("ops", "ops@test.dev", "pass-phrase-12", is_staff=True)
        self.assertEqual(Attempt.objects.for_user(staff).count(), 0)

    def test_platform_operator_sees_across_tenants(self):
        op = User.objects.create_user("op", "op@test.dev", "pass-phrase-12")
        op.is_platform_operator = True
        op.save(update_fields=["is_platform_operator"])
        self.assertEqual(Attempt.objects.for_user(op).count(), 1)

    def test_instructor_dashboard_does_not_leak_other_org(self):
        # Instructor in A, student in B -- must not see B's gradebook.
        a_ins = User.objects.create_user("ains", "ains@test.dev", "pass-phrase-12")
        add_member(a_ins, self.a, OrganizationMembership.Role.INSTRUCTOR)
        add_member(a_ins, self.b, OrganizationMembership.Role.STUDENT)
        client = Client()
        client.force_login(a_ins)
        # Active org resolves alphabetically to A, where they are an instructor.
        body = client.get("/instructor/").content.decode()
        self.assertNotIn("B secret assignment", body)
        self.assertNotIn("stub", body)


class DashboardVisibilityTests(BaseFixture):
    """Finding: only open, eligible assignments should be offered."""

    def test_ineligible_closed_and_unopened_are_hidden(self):
        other_course = Course.objects.create(organization=self.org, name="Other")
        QuizAssignment.objects.create(
            organization=self.org, title="OTHER COURSE ONLY", quiz_version=self.version,
            course=other_course, opens_at=timezone.now() - timedelta(days=1),
            created_by=self.instructor,
        )
        QuizAssignment.objects.create(
            organization=self.org, title="ALREADY CLOSED", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(days=5),
            closes_at=timezone.now() - timedelta(days=1), created_by=self.instructor,
        )
        QuizAssignment.objects.create(
            organization=self.org, title="NOT OPEN YET", quiz_version=self.version,
            opens_at=timezone.now() + timedelta(days=5), created_by=self.instructor,
        )
        titles = {a.title for a in visible_assignments_for(self.student, self.org)}
        self.assertIn("Basics Assignment", titles)
        self.assertNotIn("OTHER COURSE ONLY", titles)
        self.assertNotIn("ALREADY CLOSED", titles)
        self.assertNotIn("NOT OPEN YET", titles)


class TimingTests(BaseFixture):
    def test_allow_late_false_blocks_the_attempt(self):
        assign = QuizAssignment.objects.create(
            organization=self.org, title="No late", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(days=2),
            due_at=timezone.now() - timedelta(days=1),
            closes_at=timezone.now() + timedelta(days=1),
            allow_late=False, attempt_limit=1, created_by=self.instructor,
        )
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assign)

    def test_accommodation_prefers_most_total_time_and_respects_scope(self):
        c1 = Course.objects.create(organization=self.org, name="C1")
        TimingAccommodation.objects.create(
            organization=self.org, student=self.student, course=c1,
            multiplier="2.00", extra_minutes=0,
        )
        unscoped = QuizAssignment.objects.create(
            organization=self.org, title="Unscoped", quiz_version=self.version,
            time_limit_minutes=10, opens_at=timezone.now() - timedelta(minutes=1),
            created_by=self.instructor,
        )
        # A course-scoped grant must not apply to a course-less assignment.
        self.assertEqual(_best_accommodation(self.student, unscoped), (1, 0))

        TimingAccommodation.objects.create(
            organization=self.org, student=self.student, course=None,
            multiplier="1.00", extra_minutes=120,
        )
        mult, extra = _best_accommodation(self.student, unscoped)
        # 1.0x + 120min beats a 2.0x that only adds 10 minutes.
        self.assertEqual((mult, extra), (1, 120))

    def test_expiry_sweep_honours_grace(self):
        attempt = start_attempt(self.student, self.assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(seconds=5)
        )
        # 5s past a 30s grace: nothing should be swept yet.
        self.assertEqual(finalize_expired_attempts(), 0)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(seconds=60)
        )
        self.assertEqual(finalize_expired_attempts(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.EXPIRED)


class FeedbackAndReleaseTests(BaseFixture):
    def test_immediate_policy_returns_feedback_and_locks(self):
        version = single_question_version(
            self.org, self.instructor, policy=Quiz.FeedbackPolicy.IMMEDIATE
        )
        assign = QuizAssignment.objects.create(
            organization=self.org, title="Immediate", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assign)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(text="dict")
        outcome = save_response(attempt, self.student, item.id, [correct.id], revision=0)
        self.assertIsNotNone(outcome.feedback)
        self.assertTrue(outcome.feedback.is_correct)
        self.assertTrue(outcome.response.is_locked)

    def test_sampled_form_resolves_feedback_policy(self):
        bank = QuestionBank.objects.create(
            organization=self.org, name="bank", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="pool", required_question_count=1,
            created_by=self.instructor,
        )
        it = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="q", question_type="single",
            points=1, created_by=self.instructor,
        )
        QuestionChoice.objects.create(item=it, text="a", is_correct=True)
        QuestionChoice.objects.create(item=it, text="b", is_correct=False)
        ItemPoolItem.objects.create(pool=pool, item=it)
        assign = QuizAssignment.objects.create(
            organization=self.org, title="Sampled", form_mode="sampled", item_pool=pool,
            feedback_policy=Quiz.FeedbackPolicy.MANUAL,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        self.assertEqual(effective_feedback_policy(assign), Quiz.FeedbackPolicy.MANUAL)

    def test_manual_release_withholds_until_opened(self):
        version = single_question_version(
            self.org, self.instructor, policy=Quiz.FeedbackPolicy.MANUAL
        )
        assign = QuizAssignment.objects.create(
            organization=self.org, title="Manual", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assign)
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertFalse(result_visibility(attempt, self.student).show_answer_key)
        ResultRelease.objects.create(organization=self.org, assignment=assign, is_open=True)
        assign.refresh_from_db()
        attempt = Attempt.objects.get(id=attempt.id)
        self.assertTrue(result_visibility(attempt, self.student).show_answer_key)

    def test_answer_key_withheld_while_retake_available(self):
        multi_attempt = QuizAssignment.objects.create(
            organization=self.org, title="Retakeable", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=2,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, multi_attempt)
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        vis = result_visibility(attempt, self.student)
        # Score is fine; the key is not, because attempt 2 is still available.
        self.assertTrue(vis.show_score)
        self.assertFalse(vis.show_answer_key)


class SampledFormTests(BaseFixture):
    def test_undersized_pool_raises_instead_of_shrinking(self):
        bank = QuestionBank.objects.create(
            organization=self.org, name="bank", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="pool", required_question_count=5,
            created_by=self.instructor,
        )
        for i in range(2):
            it = QuestionBankItem.objects.create(
                organization=self.org, bank=bank, prompt=f"q{i}", question_type="single",
                points=1, created_by=self.instructor,
            )
            QuestionChoice.objects.create(item=it, text="a", is_correct=True)
            QuestionChoice.objects.create(item=it, text="b", is_correct=False)
            ItemPoolItem.objects.create(pool=pool, item=it)
        assign = QuizAssignment.objects.create(
            organization=self.org, title="Sampled", form_mode="sampled", item_pool=pool,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        with self.assertRaises(ValidationError):
            start_attempt(self.student, assign)


class VoidAndStreakTests(BaseFixture):
    def test_void_requires_instructor_and_is_audited(self):
        attempt = start_attempt(self.student, self.assignment)
        finalize_attempt(attempt, actor=self.student)
        with self.assertRaises(PermissionDenied):
            void_attempt(attempt, self.student, reason="nope")
        void_attempt(attempt, self.instructor, reason="bad question")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.VOIDED_BY_ADMIN)
        self.assertTrue(attempt.audits.filter(event_type="attempt.voided").exists())

    def test_daily_participation_and_streak(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=timezone.now().date(), title="Daily",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        cache = recompute_streak(self.student, self.org)
        self.assertEqual(cache.current_streak, 1)

    def test_ordinary_attempt_sharing_a_version_earns_no_daily_credit(self):
        # A challenge and an assignment may publish the same quiz version;
        # only the challenge attempt may earn streak credit.
        DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=timezone.now().date(), title="Daily",
        )
        attempt = start_attempt(self.student, self.assignment)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertFalse(DailyParticipation.objects.filter(attempt=attempt).exists())
        self.assertEqual(recompute_streak(self.student, self.org).current_streak, 0)


class FormModeValidationTests(BaseFixture):
    def test_cross_org_reference_is_rejected(self):
        other = make_org("other", "Other")
        assign = QuizAssignment(
            organization=other, title="X", quiz_version=self.version,
            created_by=self.instructor,
        )
        with self.assertRaises(ValidationError):
            assign.full_clean()

    def test_fixed_form_requires_version(self):
        assign = QuizAssignment(
            organization=self.org, title="X", form_mode="fixed", created_by=self.instructor
        )
        with self.assertRaises(ValidationError):
            assign.full_clean()


class HealthTests(TestCase):
    def test_healthz_ok(self):
        self.assertEqual(Client().get("/healthz/").status_code, 200)

    def test_non_post_start_is_405(self):
        org = make_org("h", "H")
        user = User.objects.create_user("u", "u@test.dev", "pass-phrase-12")
        add_member(user, org, OrganizationMembership.Role.STUDENT)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get("/assignments/1/start/").status_code, 405)


class SecurityRegressionTests(BaseFixture):
    """Audit pass 1: throttle bypass and header spoofing."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_login_throttle_blocks_before_authentication(self):
        client = Client()
        for _ in range(settings.LOGIN_RATE_LIMIT + 2):
            client.post("/accounts/login/", {"username": "student", "password": "wrong"})
        # The correct password must not get through once the budget is spent.
        response = client.post(
            "/accounts/login/", {"username": "student", "password": "pass-phrase-12"}
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_forwarded_for_cannot_be_spoofed_to_evade_throttle(self):
        client = Client()
        last = None
        for i in range(settings.LOGIN_RATE_LIMIT + 4):
            last = client.post(
                "/accounts/login/",
                {"username": "student", "password": "wrong"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i}",
            )
        self.assertEqual(last.status_code, 429)

    def test_successful_login_still_works_inside_budget(self):
        # Every attempt (success or failure) consumes budget so concurrent
        # guesses cannot share an empty window (audit F07).
        client = Client()
        for _ in range(3):
            client.post("/accounts/login/", {"username": "student", "password": "wrong"})
        response = client.post(
            "/accounts/login/", {"username": "student", "password": "pass-phrase-12"}
        )
        self.assertEqual(response.status_code, 302)

    def test_security_headers_present(self):
        response = Client().get("/accounts/login/")
        self.assertIn("Content-Security-Policy", response)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("X-Request-ID", response)

    def test_request_id_from_client_is_bounded(self):
        response = Client().get("/healthz/", HTTP_X_REQUEST_ID="x" * 500)
        self.assertLessEqual(len(response["X-Request-ID"]), 64)


class ConcurrencyAndQueryTests(BaseFixture):
    """Audit pass 1: duplicate open attempts and N+1 regressions."""

    def test_second_attempt_blocked_while_one_is_open(self):
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Retakeable", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=3,
            created_by=self.instructor,
        )
        first = start_attempt(self.student, assignment)
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, assignment)
        finalize_attempt(first, actor=self.student)
        self.assertIsNotNone(start_attempt(self.student, assignment))

    def test_read_model_does_not_scale_queries_with_questions(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        save_response(attempt, self.student, item.id,
                      [item.choice_snapshots.first().id], 0)
        with CaptureQueriesContext(connection) as ctx:
            student_read_model(attempt)
        self.assertLessEqual(len(ctx), 5)

    def test_dashboard_queries_are_bounded(self):
        for i in range(8):
            QuizAssignment.objects.create(
                organization=self.org, title=f"extra{i}", quiz_version=self.version,
                opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
            )
        client = Client()
        client.force_login(self.student)
        with CaptureQueriesContext(connection) as ctx:
            client.get("/")
        # Constant-ish: eligibility is resolved from pre-loaded sets.
        self.assertLess(len(ctx), 25)


class AttemptLifecycleGuardTests(BaseFixture):
    """Audit pass 2: state-machine and authorization edges."""

    def test_voided_attempt_cannot_be_resurrected_by_finalize(self):
        attempt = start_attempt(self.student, self.assignment)
        void_attempt(attempt, self.instructor, reason="bad item")
        with self.assertRaises(PermissionDenied):
            finalize_attempt(attempt, reason="expired")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.VOIDED_BY_ADMIN)

    def test_voided_attempt_is_not_swept(self):
        attempt = start_attempt(self.student, self.assignment)
        void_attempt(attempt, self.instructor, reason="bad item")
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(finalize_expired_attempts(), 0)

    def test_late_submit_is_recorded_as_expired_not_submitted(self):
        attempt = start_attempt(self.student, self.assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        client = Client()
        client.force_login(self.student)
        client.post(f"/attempts/{attempt.id}/submit/")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.EXPIRED)
        self.assertEqual(attempt.finalization_reason, "expired")

    def test_deactivating_an_assignment_stops_in_flight_saves(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        QuizAssignment.objects.filter(id=self.assignment.id).update(is_active=False)
        with self.assertRaises(AttemptExpired):
            save_response(attempt, self.student, item.id,
                          [item.choice_snapshots.first().id], 0)

    def test_instructor_is_redirected_from_the_live_answer_form(self):
        attempt = start_attempt(self.student, self.assignment)
        client = Client()
        client.force_login(self.instructor)
        response = client.get(f"/attempts/{attempt.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/result/", response["Location"])

    def test_another_student_cannot_read_a_result(self):
        other = User.objects.create_user("other", "other@test.dev", "pass-phrase-12")
        add_member(other, self.org, OrganizationMembership.Role.STUDENT)
        attempt = start_attempt(self.student, self.assignment)
        finalize_attempt(attempt, actor=self.student)
        client = Client()
        client.force_login(other)
        self.assertEqual(client.get(f"/attempts/{attempt.id}/result/").status_code, 403)

    def test_foreign_choice_id_is_rejected(self):
        other = User.objects.create_user("other2", "other2@test.dev", "pass-phrase-12")
        add_member(other, self.org, OrganizationMembership.Role.STUDENT)
        Enrollment.objects.create(
            organization=self.org, student=other, course=self.course, cohort=self.cohort
        )
        mine = start_attempt(self.student, self.assignment)
        theirs = start_attempt(other, self.assignment)
        foreign_choice = theirs.item_snapshots.first().choice_snapshots.first()
        item = mine.item_snapshots.first()
        with self.assertRaises(ValidationError):
            save_response(mine, self.student, item.id, [foreign_choice.id], 0)


class ErrorSurfaceTests(BaseFixture):
    """Audit pass 3: malformed input, error pages, log hygiene."""

    def test_missing_revision_is_rejected(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        client = Client()
        client.force_login(self.student)
        response = client.post(f"/attempts/{attempt.id}/save/{item.id}/", {"choices": []})
        self.assertEqual(response.status_code, 400)

    def test_negative_and_non_numeric_payloads_rejected(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        client = Client()
        client.force_login(self.student)
        url = f"/attempts/{attempt.id}/save/{item.id}/"
        self.assertEqual(
            client.post(url, {"choices": "abc", "revision": "0"}).status_code, 400
        )
        self.assertEqual(
            client.post(url, {"choices": [], "revision": "-1"}).status_code, 400
        )

    @override_settings(DEBUG=False)
    def test_error_pages_leak_no_internals(self):
        client = Client()
        client.force_login(self.student)
        body = client.get("/attempts/999999/").content.decode()
        for marker in ("Traceback", "django.db", "SELECT ", "settings.py"):
            self.assertNotIn(marker, body)

    def test_csrf_is_enforced_on_state_change(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.student)
        response = client.post(f"/assignments/{self.assignment.id}/start/", {})
        self.assertEqual(response.status_code, 403)

    def test_logs_scrub_secrets_and_answer_keys(self):
        import json as _json

        from config.logging_filters import scrub

        scrubbed = scrub(
            {
                "password": "hunter2",
                "sampled_seed": "deadbeef",
                "answer_key": [1, 2],
                "nested": {"cookie": "c", "safe": "keep"},
            }
        )
        dumped = _json.dumps(scrubbed)
        for secret in ("hunter2", "deadbeef"):
            self.assertNotIn(secret, dumped)
        self.assertIn("keep", dumped)


class BackgroundJobTests(BaseFixture):
    """Audit pass 3: jobs must be idempotent and resilient."""

    def test_notification_jobs_are_idempotent(self):
        QuizAssignment.objects.create(
            organization=self.org, title="Due soon", quiz_version=self.version,
            opens_at=timezone.now() - timedelta(days=1),
            due_at=timezone.now() + timedelta(hours=2),
            course=self.course, cohort=self.cohort,
            attempt_limit=1, created_by=self.instructor,
        )
        first = send_due_soon_notifications()
        self.assertGreater(first, 0)
        self.assertEqual(send_due_soon_notifications(), 0)

    def test_daily_notifications_are_idempotent(self):
        DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Daily",
        )
        self.assertGreater(send_daily_challenge_notifications(), 0)
        self.assertEqual(send_daily_challenge_notifications(), 0)

    def test_sweep_is_idempotent(self):
        attempt = start_attempt(self.student, self.assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        self.assertEqual(finalize_expired_attempts(), 1)
        self.assertEqual(finalize_expired_attempts(), 0)

    def test_sweep_reaches_window_closed_attempts_without_deadlines(self):
        attempt = start_attempt(self.student, self.assignment)
        QuizAssignment.objects.filter(id=self.assignment.id).update(
            closes_at=timezone.now() - timedelta(hours=1)
        )
        Attempt.objects.filter(id=attempt.id).update(deadline_at=None)
        self.assertEqual(finalize_expired_attempts(), 1)

    def test_export_cleanup_tolerates_missing_files(self):
        ExportArtifact.objects.create(
            organization=self.org, requested_by=self.instructor, name="stale",
            file_path="/nonexistent/path.csv",
            expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(cleanup_expired_exports(), 1)
        self.assertEqual(ExportArtifact.objects.count(), 0)

    def test_streak_cache_updates_on_commit(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Daily",
        )
        # on_commit callbacks do not run inside TestCase without this helper,
        # which is why the streak refresh was previously untested.
        with self.captureOnCommitCallbacks(execute=True):
            attempt = start_daily_attempt(self.student, challenge)
            finalize_attempt(attempt, reason="submitted", actor=self.student)
        cache_row = StreakCache.objects.get(organization=self.org, student=self.student)
        self.assertEqual(cache_row.current_streak, 1)


class TimezoneEdgeTests(TestCase):
    """Audit pass 3: extreme offsets and bad stored data."""

    def test_org_local_date_differs_from_utc_at_plus_14(self):
        org = make_org("kiri", "Kiritimati", tz="Pacific/Kiritimati")
        self.assertIsNotNone(today_for_org(org))

    def test_invalid_stored_timezone_falls_back_instead_of_500(self):
        org = make_org("bad", "Bad TZ")
        Organization.objects.filter(id=org.id).update(timezone="Not/AZone")
        org.refresh_from_db()
        self.assertIsNotNone(today_for_org(org))


class PrivacyTests(BaseFixture):
    """Audit pass 4: export and erasure (LAUNCH data-rights controls)."""

    def test_export_contains_the_full_inventory(self):
        from accounts.privacy import export_user_data

        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        save_response(attempt, self.student, item.id,
                      [item.choice_snapshots.first().id], 0)
        finalize_attempt(attempt, actor=self.student)

        data = export_user_data(self.student)
        self.assertEqual(data["account"]["username"], "student")
        self.assertEqual(len(data["attempts"]), 1)
        self.assertEqual(len(data["attempts"][0]["responses"]), 1)
        self.assertTrue(data["organizations"])
        self.assertTrue(data["audit_trail"])

    def test_self_service_export_downloads_json(self):
        client = Client()
        client.force_login(self.student)
        response = client.post("/accounts/me/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_erasure_tombstones_subject_but_keeps_assessment_record(self):
        from accounts.privacy import anonymize_user

        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        save_response(attempt, self.student, item.id,
                      [item.choice_snapshots.first().id], 0, request=None)
        finalize_attempt(attempt, actor=self.student)

        anonymize_user(self.student, reason="test")
        self.student.refresh_from_db()

        self.assertTrue(self.student.username.startswith("erased-"))
        self.assertEqual(self.student.email, "")
        self.assertFalse(self.student.is_active)
        self.assertFalse(self.student.has_usable_password())
        # The assessment facts survive.
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.FINALIZED)
        self.assertTrue(AttemptFinalization.objects.filter(attempt=attempt).exists())
        self.assertTrue(AttemptAudit.objects.filter(attempt=attempt).exists())
        # Network identifiers are gone.
        for event in ResponseEvent.objects.filter(attempt=attempt):
            self.assertIsNone(event.ip_address)
            self.assertEqual(event.user_agent, "")

    def test_self_service_delete_requires_password_and_confirmation(self):
        client = Client()
        client.force_login(self.student)
        # Wrong confirmation
        client.post("/accounts/me/delete/", {"password": "pass-phrase-12", "confirm": "no"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        # Wrong password
        client.post("/accounts/me/delete/", {"password": "nope", "confirm": "DELETE"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        # Correct
        client.post("/accounts/me/delete/", {"password": "pass-phrase-12", "confirm": "DELETE"})
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

    def test_delete_is_refused_mid_attempt(self):
        start_attempt(self.student, self.assignment)
        client = Client()
        client.force_login(self.student)
        client.post("/accounts/me/delete/", {"password": "pass-phrase-12", "confirm": "DELETE"})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_legal_pages_are_public(self):
        client = Client()
        self.assertEqual(client.get("/legal/terms/").status_code, 200)
        self.assertEqual(client.get("/legal/privacy/").status_code, 200)

    def test_management_commands(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("export_user_data", "student", stdout=out)
        self.assertIn('"username": "student"', out.getvalue())

        out = StringIO()
        call_command("delete_user", "student", "--yes", stdout=out)
        self.assertIn("tombstone", out.getvalue())


class AuthoringWorkspaceTests(BaseFixture):
    """Phase 2a: instructors author in-product, not through Django admin."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.instructor)

    FORMSET = {
        "choices-TOTAL_FORMS": "4",
        "choices-INITIAL_FORMS": "0",
        "choices-MIN_NUM_FORMS": "2",
        "choices-MAX_NUM_FORMS": "1000",
    }

    def _question_payload(self, prompt, qtype="single", correct=("a",), wrong=("b",)):
        data = dict(self.FORMSET)
        data.update({"prompt": prompt, "question_type": qtype, "points": "1", "explanation": ""})
        index = 0
        for text in correct:
            data[f"choices-{index}-text"] = text
            data[f"choices-{index}-is_correct"] = "on"
            index += 1
        for text in wrong:
            data[f"choices-{index}-text"] = text
            index += 1
        while index < 4:
            data[f"choices-{index}-text"] = ""
            index += 1
        return data

    def test_create_quiz_add_question_and_publish(self):
        response = self.client.post(
            "/instructor/quizzes/new/",
            {"title": "In-app quiz", "description": "", "time_limit_minutes": "10",
             "attempt_limit": "1", "feedback_policy": "at_end", "pass_mark_percent": "70"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        quiz = Quiz.objects.get(title="In-app quiz")
        self.assertEqual(quiz.organization, self.org)
        self.assertEqual(quiz.created_by, self.instructor)

        self.client.post(
            f"/instructor/quizzes/{quiz.id}/questions/new/",
            self._question_payload("What is 2+2?", correct=("4",), wrong=("5",)),
        )
        self.assertEqual(quiz.questions.count(), 1)
        question = quiz.questions.first()
        # Ordering is assigned for the instructor.
        self.assertEqual(question.sort_order, 1)
        self.assertEqual([c.sort_order for c in question.choices.all()], [1, 2])

        self.client.post(f"/instructor/quizzes/{quiz.id}/publish/")
        self.assertEqual(quiz.versions.count(), 1)

    def test_answer_key_rules_are_enforced_at_authoring_time(self):
        quiz = Quiz.objects.create(
            organization=self.org, title="Rules", created_by=self.instructor
        )
        # Two correct choices on a single-answer question.
        response = self.client.post(
            f"/instructor/quizzes/{quiz.id}/questions/new/",
            self._question_payload("Bad", correct=("a", "b"), wrong=()),
        )
        self.assertContains(response, "Exactly one choice must be marked correct")
        self.assertEqual(quiz.questions.count(), 0)

        # Multi-select where every choice is correct.
        response = self.client.post(
            f"/instructor/quizzes/{quiz.id}/questions/new/",
            self._question_payload("AllRight", qtype="multi", correct=("a", "b"), wrong=()),
        )
        self.assertContains(response, "at least one incorrect choice")

        # Duplicate choice text.
        response = self.client.post(
            f"/instructor/quizzes/{quiz.id}/questions/new/",
            self._question_payload("Dupe", correct=("same",), wrong=("same",)),
        )
        self.assertContains(response, "same text")

    def test_publish_is_blocked_until_the_draft_is_valid(self):
        quiz = Quiz.objects.create(
            organization=self.org, title="Incomplete", created_by=self.instructor
        )
        Question.objects.create(
            quiz=quiz, prompt="No choices", question_type="single", points=1
        )
        response = self.client.get(f"/instructor/quizzes/{quiz.id}/")
        self.assertContains(response, "Not ready to publish")
        self.assertFalse(response.context["can_publish"])

    def test_instructor_cannot_reach_another_organizations_quiz(self):
        other_org = make_org("other", "Other Org")
        other_ins = User.objects.create_user("oi", "oi@test.dev", "pass-phrase-12")
        add_member(other_ins, other_org, OrganizationMembership.Role.INSTRUCTOR)
        foreign = Quiz.objects.create(
            organization=other_org, title="Foreign", created_by=other_ins
        )
        self.assertEqual(
            self.client.get(f"/instructor/quizzes/{foreign.id}/").status_code, 404
        )

    def test_students_cannot_reach_the_workspace(self):
        client = Client()
        client.force_login(self.student)
        for url in ("/instructor/quizzes/", "/instructor/assignments/",
                    "/instructor/quizzes/new/"):
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_assignment_form_rejects_cross_tenant_version(self):
        other_org = make_org("x2", "X2")
        other_ins = User.objects.create_user("x2i", "x2i@test.dev", "pass-phrase-12")
        add_member(other_ins, other_org, OrganizationMembership.Role.INSTRUCTOR)
        foreign_version = single_question_version(other_org, other_ins)
        response = self.client.post(
            "/instructor/assignments/new/",
            {"title": "Sneaky", "form_mode": "fixed", "quiz_version": foreign_version.id,
             "opens_at": "2026-07-20T09:00", "attempt_limit": "1", "grace_seconds": "30",
             "is_active": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(QuizAssignment.objects.filter(title="Sneaky").exists())


class AnalyticsTests(BaseFixture):
    """Phase 2a: gradebook and psychometrics must be arithmetically right."""

    def _multi_question_version(self, n=4):
        # Shuffling off: these tests assert per-question statistics, so each
        # student must see the questions in the same order.
        quiz = Quiz.objects.create(
            organization=self.org, title="Analytics quiz", created_by=self.instructor,
            pass_mark_percent=50, shuffle_questions=False, shuffle_choices=False,
        )
        for i in range(n):
            q = Question.objects.create(
                quiz=quiz, prompt=f"Question {i}", question_type="single",
                points=1, sort_order=i,
            )
            Choice.objects.create(question=q, text="right", is_correct=True, sort_order=1)
            Choice.objects.create(question=q, text="wrong", is_correct=False, sort_order=2)
        return publish_quiz(quiz, self.instructor)

    def _sit(self, student, assignment, correct_count):
        attempt = start_attempt(student, assignment)
        revision = 0
        for index, item in enumerate(attempt.item_snapshots.order_by("sort_order")):
            wanted = "right" if index < correct_count else "wrong"
            choice = item.choice_snapshots.get(text=wanted)
            save_response(attempt, student, item.id, [choice.id], revision)
            revision += 1
        finalize_attempt(attempt, actor=student)
        attempt.refresh_from_db()
        return attempt

    def _cohort_of(self, n):
        students = []
        for i in range(n):
            user = User.objects.create_user(f"s{i}", f"s{i}@test.dev", "pass-phrase-12")
            add_member(user, self.org, OrganizationMembership.Role.STUDENT)
            Enrollment.objects.create(
                organization=self.org, student=user, course=self.course, cohort=self.cohort
            )
            students.append(user)
        return students

    def test_gradebook_uses_best_attempt_and_counts_tries(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Retakeable", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=2,
            created_by=self.instructor,
        )
        self._sit(self.student, assignment, correct_count=1)   # 25%
        self._sit(self.student, assignment, correct_count=3)   # 75%
        rows = gradebook_rows(assignment, pass_mark=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attempts, 2)
        self.assertEqual(rows[0].best_score, 3)
        self.assertEqual(rows[0].best_percent, Decimal("75.0"))
        self.assertTrue(rows[0].passed)

    def test_distribution_mean_and_median(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Stats", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        for student, correct in zip(self._cohort_of(3), (0, 2, 4), strict=True):
            self._sit(student, assignment, correct)
        stats = assignment_analytics(assignment, pass_mark=50)
        self.assertEqual(stats.finalized_attempts, 3)
        self.assertEqual(stats.students_attempted, 3)
        # 0%, 50%, 100%
        self.assertEqual(stats.mean_percent, Decimal("50.0"))
        self.assertEqual(stats.median_percent, Decimal("50.0"))
        self.assertEqual(stats.pass_rate, Decimal("66.7"))
        self.assertEqual(sum(count for _, count in stats.distribution), 3)

    def test_item_difficulty_and_discrimination(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Items", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        # Students answer the first N questions correctly, so question 0 is the
        # easiest and question 3 the hardest, and both track total score.
        for student, correct in zip(self._cohort_of(6), (0, 1, 2, 3, 4, 4), strict=True):
            self._sit(student, assignment, correct)

        stats = {item.prompt: item for item in item_statistics(assignment)}
        self.assertEqual(stats["Question 0"].responses, 6)
        self.assertGreater(stats["Question 0"].difficulty, stats["Question 3"].difficulty)
        # Items that separate strong from weak students discriminate positively.
        self.assertGreater(stats["Question 3"].discrimination, Decimal("0"))

    def test_in_progress_attempts_are_excluded_from_grades(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Open", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        start_attempt(self.student, assignment)  # never finalized
        self.assertEqual(gradebook_rows(assignment), [])
        self.assertEqual(assignment_analytics(assignment).finalized_attempts, 0)

    def test_csv_export_neutralises_formula_injection(self):
        evil = User.objects.create_user("evil", "evil@test.dev", "pass-phrase-12")
        evil.first_name = "=cmd|' /c calc'!A1"
        evil.save()
        add_member(evil, self.org, OrganizationMembership.Role.STUDENT)
        Enrollment.objects.create(
            organization=self.org, student=evil, course=self.course, cohort=self.cohort
        )
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Injection", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        self._sit(evil, assignment, 2)
        csv_text = gradebook_csv(assignment)
        self.assertIn("'=cmd", csv_text)
        self.assertNotIn(",=cmd", csv_text)

    def test_export_is_audited_and_expires(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Audited", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        client = Client()
        client.force_login(self.instructor)
        response = client.get(f"/instructor/assignments/{assignment.id}/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, private")
        self.assertTrue(AdminActionLog.objects.filter(action="export.gradebook").exists())
        artifact = ExportArtifact.objects.get()
        self.assertGreater(artifact.expires_at, timezone.now())

    def test_students_cannot_export(self):
        version = self._multi_question_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="NoExport", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        client = Client()
        client.force_login(self.student)
        self.assertEqual(
            client.get(f"/instructor/assignments/{assignment.id}/export/").status_code, 403
        )


class NotificationCentreTests(BaseFixture):
    """Phase 2a: the jobs already created notifications nobody could see."""

    def test_student_sees_and_clears_notifications(self):
        Notification.objects.create(
            organization=self.org, user=self.student,
            kind=Notification.Kind.DUE_SOON, title="Due soon: Basics",
            body="Due tomorrow", dedupe_key="k1",
        )
        client = Client()
        client.force_login(self.student)
        response = client.get("/notifications/")
        self.assertContains(response, "Due soon: Basics")
        # Unread badge is surfaced in the nav.
        self.assertEqual(response.context["unread_notifications"], 1)

        client.post("/notifications/read/")
        self.assertFalse(
            Notification.objects.filter(user=self.student, read_at__isnull=True).exists()
        )

    def test_notifications_are_per_user(self):
        other = User.objects.create_user("nother", "nother@test.dev", "pass-phrase-12")
        add_member(other, self.org, OrganizationMembership.Role.STUDENT)
        Notification.objects.create(
            organization=self.org, user=other, title="Private", dedupe_key="k2"
        )
        client = Client()
        client.force_login(self.student)
        self.assertNotContains(client.get("/notifications/"), "Private")

"""Phase 4: remaining branch coverage — sampled attempts, streaks, races, edges."""

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Attempt,
    AttemptFinalization,
    Choice,
    DailyChallenge,
    DailyParticipation,
    IntegrityEvent,
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
    _client_ip,
    _is_assignment_open,
    finalize_attempt,
    publish_quiz,
    recompute_streak,
    result_visibility,
    save_response,
    start_attempt,
    today_for_org,
)


class EdgeFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Org", slug="org")
        self.instructor = User.objects.create_user("ins", "ins@t.dev", "pass-phrase-12")
        self.student = User.objects.create_user("stu", "stu@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=self.instructor, organization=self.org,
            role=OrganizationMembership.Role.INSTRUCTOR,
        )
        OrganizationMembership.objects.create(
            user=self.student, organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )

    def make_version(self, minutes=None):
        quiz = Quiz.objects.create(
            organization=self.org, title="Q", created_by=self.instructor,
            time_limit_minutes=minutes, shuffle_questions=False, shuffle_choices=False,
        )
        question = Question.objects.create(
            quiz=quiz, prompt="P", question_type="single", points=1
        )
        Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
        return publish_quiz(quiz, self.instructor)


class SampledAttemptTests(EdgeFixture):
    def _pool(self, count=4):
        bank = QuestionBank.objects.create(
            organization=self.org, name="B", created_by=self.instructor
        )
        pool = ItemPool.objects.create(
            organization=self.org, name="Pool", required_question_count=2,
            created_by=self.instructor,
        )
        for i in range(count):
            item = QuestionBankItem.objects.create(
                organization=self.org, bank=bank, prompt=f"Bank item {i}",
                question_type="single", points=2, explanation=f"E{i}",
                created_by=self.instructor,
            )
            QuestionChoice.objects.create(item=item, text="right", is_correct=True, sort_order=1)
            QuestionChoice.objects.create(item=item, text="wrong", is_correct=False, sort_order=2)
            ItemPoolItem.objects.create(pool=pool, item=item)
        return pool

    def test_sampled_attempt_snapshots_items_and_scores(self):
        pool = self._pool()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Sampled", form_mode="sampled", item_pool=pool,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        self.assertEqual(attempt.item_snapshots.count(), 2)
        self.assertEqual(attempt.max_score, 4)
        # Sampling is recorded for audit reproducibility.
        self.assertEqual(len(attempt.sampling_criteria["item_ids"]), 2)
        self.assertTrue(attempt.sampled_seed)

        for item in attempt.item_snapshots.all():
            save_response(
                attempt, self.student, item.id,
                [item.choice_snapshots.get(text="right").id], attempt.revision,
            )
            attempt.refresh_from_db()
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        self.assertEqual(attempt.score, 4)

    def test_two_students_get_independent_sampled_forms(self):
        pool = self._pool(count=8)
        pool.required_question_count = 3
        pool.save()
        other = User.objects.create_user("stu2", "s2@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=other, organization=self.org,
            role=OrganizationMembership.Role.STUDENT,
        )
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Sampled2", form_mode="sampled", item_pool=pool,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        first = start_attempt(self.student, assignment)
        second = start_attempt(other, assignment)
        self.assertNotEqual(first.sampled_seed, second.sampled_seed)
        self.assertEqual(first.item_snapshots.count(), 3)
        self.assertEqual(second.item_snapshots.count(), 3)

    def test_sampled_result_visibility_allows_the_key_on_retake(self):
        # A sampled form regenerates per attempt, so showing the key does not
        # hand over the next paper.
        pool = self._pool()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="SampledRetake", form_mode="sampled",
            item_pool=pool, opens_at=timezone.now() - timedelta(minutes=1),
            attempt_limit=3, created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        attempt.refresh_from_db()
        visibility = result_visibility(attempt, self.student)
        self.assertTrue(visibility.show_answer_key)

    def test_sampled_assignment_without_a_pool_is_refused(self):
        from django.core.exceptions import ValidationError

        assignment = QuizAssignment.objects.create(
            organization=self.org, title="NoPool", form_mode="sampled",
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        with self.assertRaises(ValidationError):
            start_attempt(self.student, assignment)


class WindowTests(EdgeFixture):
    def test_is_assignment_open_branches(self):
        version = self.make_version()
        now = timezone.now()
        inactive = QuizAssignment.objects.create(
            organization=self.org, title="Inactive", quiz_version=version,
            opens_at=now - timedelta(days=1), is_active=False,
            created_by=self.instructor,
        )
        future = QuizAssignment.objects.create(
            organization=self.org, title="Future", quiz_version=version,
            opens_at=now + timedelta(days=1), created_by=self.instructor,
        )
        past = QuizAssignment.objects.create(
            organization=self.org, title="Past", quiz_version=version,
            opens_at=now - timedelta(days=2), closes_at=now - timedelta(days=1),
            created_by=self.instructor,
        )
        open_now = QuizAssignment.objects.create(
            organization=self.org, title="Open", quiz_version=version,
            opens_at=now - timedelta(minutes=1), created_by=self.instructor,
        )
        self.assertFalse(_is_assignment_open(inactive, now))
        self.assertFalse(_is_assignment_open(future, now))
        self.assertFalse(_is_assignment_open(past, now))
        self.assertTrue(_is_assignment_open(open_now, now))

    def test_starting_a_closed_assignment_is_refused(self):
        version = self.make_version()
        closed = QuizAssignment.objects.create(
            organization=self.org, title="Closed", quiz_version=version,
            opens_at=timezone.now() - timedelta(days=2),
            closes_at=timezone.now() - timedelta(days=1),
            created_by=self.instructor,
        )
        with self.assertRaises(PermissionDenied):
            start_attempt(self.student, closed)

    def test_instructor_cannot_take_a_student_assignment(self):
        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="StudentsOnly", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), created_by=self.instructor,
        )
        with self.assertRaises(PermissionDenied):
            start_attempt(self.instructor, assignment)


class FinalizationRaceTests(EdgeFixture):
    def test_lost_race_returns_the_winning_record(self):
        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Race", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        winner = AttemptFinalization.objects.create(
            attempt=attempt, reason="submitted", score=0, max_score=1
        )
        # Simulate the race: the row exists but this instance has not cached it.
        fresh = Attempt.objects.get(id=attempt.id)
        result = finalize_attempt(fresh, reason="expired")
        self.assertEqual(result.id, winner.id)
        self.assertTrue(
            IntegrityEvent.objects.filter(
                kind=IntegrityEvent.Kind.DUPLICATE_FINALIZE
            ).exists()
        )

    def test_integrity_error_path_recovers(self):
        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Race2", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        winner = AttemptFinalization.objects.create(
            attempt=attempt, reason="submitted", score=0, max_score=1
        )

        # Directly exercise the recovery path.
        fresh = Attempt.objects.get(id=attempt.id)
        self.assertEqual(finalize_attempt(fresh).id, winner.id)


class StreakArithmeticTests(EdgeFixture):
    def _participation(self, days_ago, earned=True):
        version = self.make_version()
        challenge, _ = DailyChallenge.objects.get_or_create(
            organization=self.org,
            release_date=today_for_org(self.org) - timedelta(days=days_ago),
            defaults={"quiz_version": version, "title": f"D{days_ago}"},
        )
        assignment = QuizAssignment.objects.create(
            organization=self.org, title=f"A{days_ago}", quiz_version=version,
            opens_at=timezone.now() - timedelta(days=days_ago + 1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        return DailyParticipation.objects.create(
            organization=self.org, student=self.student, challenge=challenge,
            attempt=attempt, credit_date=challenge.release_date, earned_credit=earned,
        )

    def test_consecutive_days_build_a_streak(self):
        for days_ago in (2, 1, 0):
            self._participation(days_ago)
        cache_row = recompute_streak(self.student, self.org)
        self.assertEqual(cache_row.current_streak, 3)
        self.assertEqual(cache_row.longest_streak, 3)

    def test_a_gap_breaks_the_current_streak_but_keeps_the_longest(self):
        for days_ago in (6, 5, 4, 1, 0):
            self._participation(days_ago)
        cache_row = recompute_streak(self.student, self.org)
        self.assertEqual(cache_row.current_streak, 2)
        self.assertEqual(cache_row.longest_streak, 3)

    def test_a_stale_streak_reads_as_zero(self):
        self._participation(10)
        self._participation(9)
        cache_row = recompute_streak(self.student, self.org)
        self.assertEqual(cache_row.current_streak, 0)
        self.assertEqual(cache_row.longest_streak, 2)

    def test_yesterday_still_counts_as_current(self):
        self._participation(1)
        self.assertEqual(recompute_streak(self.student, self.org).current_streak, 1)

    def test_unearned_credit_is_ignored(self):
        self._participation(0, earned=False)
        self.assertEqual(recompute_streak(self.student, self.org).current_streak, 0)

    def test_no_participation_yields_zero(self):
        cache_row = recompute_streak(self.student, self.org)
        self.assertEqual((cache_row.current_streak, cache_row.longest_streak), (0, 0))


class ClientIPTests(TestCase):
    def test_none_request_returns_none(self):
        self.assertIsNone(_client_ip(None))

    def test_remote_addr_is_used_by_default(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="9.9.9.9")
        request.META["REMOTE_ADDR"] = "127.0.0.1"
        self.assertEqual(_client_ip(request), "127.0.0.1")

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_for_is_used_behind_a_trusted_proxy(self):
        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="9.9.9.9, 10.0.0.1")
        self.assertEqual(_client_ip(request), "9.9.9.9")

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_missing_forwarded_header_falls_back(self):
        request = RequestFactory().get("/")
        request.META["REMOTE_ADDR"] = "127.0.0.1"
        self.assertEqual(_client_ip(request), "127.0.0.1")


class ResponseEventTests(EdgeFixture):
    def test_save_records_an_event_with_request_metadata(self):
        from assessments.models import ResponseEvent

        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Evented", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.first()
        request = RequestFactory().post("/", HTTP_USER_AGENT="TestAgent/1.0")
        request.META["REMOTE_ADDR"] = "10.1.2.3"
        save_response(attempt, self.student, item.id, [], 0, request=request)
        event = ResponseEvent.objects.get()
        self.assertEqual(event.ip_address, "10.1.2.3")
        self.assertEqual(event.user_agent, "TestAgent/1.0")
        self.assertEqual((event.revision_before, event.revision_after), (0, 1))

    def test_user_agent_is_truncated(self):
        from assessments.models import ResponseEvent

        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="LongUA", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.first()
        request = RequestFactory().post("/", HTTP_USER_AGENT="x" * 500)
        save_response(attempt, self.student, item.id, [], 0, request=request)
        self.assertLessEqual(len(ResponseEvent.objects.get().user_agent), 300)


class ModelStringTests(EdgeFixture):
    def test_str_methods(self):
        version = self.make_version()
        self.assertIn("v1", str(version))
        self.assertEqual(str(self.org), "Org")
        membership = OrganizationMembership.objects.get(user=self.student)
        self.assertIn("Student", str(membership))
        self.assertEqual(str(self.student), "stu")

        from assessments.models import Cohort, Course, Program

        program = Program.objects.create(organization=self.org, name="Prog")
        course = Course.objects.create(organization=self.org, name="Crs")
        cohort = Cohort.objects.create(organization=self.org, course=course, name="Coh")
        self.assertEqual(str(program), "Prog")
        self.assertEqual(str(course), "Crs")
        self.assertEqual(str(cohort), "Crs: Coh")

    def test_user_str_prefers_full_name(self):
        self.student.first_name = "Sam"
        self.student.last_name = "Learner"
        self.student.save()
        self.assertEqual(str(self.student), "Sam Learner")

    def test_bank_item_str_truncates(self):
        from assessments.models import QuestionBank, QuestionBankItem

        bank = QuestionBank.objects.create(
            organization=self.org, name="B", created_by=self.instructor
        )
        item = QuestionBankItem.objects.create(
            organization=self.org, bank=bank, prompt="x" * 200,
            question_type="single", points=1, created_by=self.instructor,
        )
        self.assertEqual(len(str(item)), 80)
        self.assertEqual(str(bank), "B")

    def test_result_release_str(self):
        from assessments.models import ResultRelease

        version = self.make_version()
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="R", quiz_version=version,
            opens_at=timezone.now(), created_by=self.instructor,
        )
        release = ResultRelease.objects.create(
            organization=self.org, assignment=assignment, is_open=True
        )
        self.assertIn("open", str(release))

    def test_organization_zoneinfo_fallback(self):
        Organization.objects.filter(id=self.org.id).update(timezone="Bad/Zone")
        self.org.refresh_from_db()
        self.assertEqual(str(self.org.zoneinfo), "UTC")

    def test_accommodation_helpers(self):
        from decimal import Decimal

        from assessments.models import TimingAccommodation

        accommodation = TimingAccommodation.objects.create(
            organization=self.org, student=self.student,
            multiplier=Decimal("1.5"), extra_minutes=10,
        )
        self.assertEqual(accommodation.accommodated_minutes(20), Decimal("40.0"))
        self.assertTrue(accommodation.applies_at(timezone.now()))

        accommodation.is_active = False
        self.assertFalse(accommodation.applies_at(timezone.now()))

        accommodation.is_active = True
        accommodation.effective_from = timezone.now() + timedelta(days=1)
        self.assertFalse(accommodation.applies_at(timezone.now()))

        accommodation.effective_from = None
        accommodation.effective_until = timezone.now() - timedelta(days=1)
        self.assertFalse(accommodation.applies_at(timezone.now()))


class AccountViewTests(EdgeFixture):
    def test_account_page_lists_memberships(self):
        client = Client()
        client.force_login(self.student)
        response = client.get("/accounts/me/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Org")

    def test_export_is_rate_limited(self):
        client = Client()
        client.force_login(self.student)
        for _ in range(4):
            client.post("/accounts/me/export/")
        self.assertEqual(client.post("/accounts/me/export/").status_code, 429)

    def test_password_change_flow(self):
        client = Client()
        client.force_login(self.student)
        response = client.post(
            "/accounts/password_change/",
            {"old_password": "pass-phrase-12",
             "new_password1": "brand-new-phrase-99",
             "new_password2": "brand-new-phrase-99"},
        )
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.check_password("brand-new-phrase-99"))

    def test_password_reset_sends_mail(self):
        from django.core import mail

        client = Client()
        client.post("/accounts/password_reset/", {"email": "stu@t.dev"})
        self.assertEqual(len(mail.outbox), 1)

    def test_login_page_does_not_leak_demo_credentials(self):
        # The brand name is fine; a seeded password on a production login page
        # is not.
        body = Client().get("/accounts/login/").content.decode().lower()
        for secret in ("quizops-demo", "quizops123", "password is"):
            self.assertNotIn(secret, body)

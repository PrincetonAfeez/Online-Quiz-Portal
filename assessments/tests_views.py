"""Phase 4: exhaustive view-layer coverage — every route, every branch."""

from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.models import (
    Attempt,
    Choice,
    Cohort,
    Course,
    DailyChallenge,
    Enrollment,
    Notification,
    Question,
    Quiz,
    QuizAssignment,
    ResultRelease,
)
from assessments.services import (
    finalize_attempt,
    publish_quiz,
    save_response,
    start_attempt,
    start_daily_attempt,
    today_for_org,
)


def build_version(org, instructor, minutes=30, questions=2, policy="at_end", shuffle=False):
    quiz = Quiz.objects.create(
        organization=org, title="Quiz", created_by=instructor,
        time_limit_minutes=minutes, feedback_policy=policy,
        shuffle_questions=shuffle, shuffle_choices=shuffle,
    )
    for i in range(questions):
        question = Question.objects.create(
            quiz=quiz, prompt=f"Prompt {i}", question_type="single", points=1, sort_order=i
        )
        Choice.objects.create(question=question, text="right", is_correct=True, sort_order=1)
        Choice.objects.create(question=question, text="wrong", is_correct=False, sort_order=2)
    return publish_quiz(quiz, instructor)


class ViewFixture(TestCase):
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
        self.course = Course.objects.create(organization=self.org, name="C")
        self.cohort = Cohort.objects.create(
            organization=self.org, course=self.course, name="Co"
        )
        Enrollment.objects.create(
            organization=self.org, student=self.student,
            course=self.course, cohort=self.cohort,
        )
        self.version = build_version(self.org, self.instructor)
        self.assignment = QuizAssignment.objects.create(
            organization=self.org, title="Assignment", quiz_version=self.version,
            course=self.course, cohort=self.cohort,
            opens_at=timezone.now() - timedelta(minutes=1),
            closes_at=timezone.now() + timedelta(days=1),
            attempt_limit=1, created_by=self.instructor,
        )
        self.client = Client()
        self.client.force_login(self.student)


class AuthenticationGateTests(ViewFixture):
    def test_every_protected_route_redirects_anonymous_users(self):
        anon = Client()
        routes = [
            "/", "/progress/", "/daily/", "/notifications/",
            "/instructor/", "/instructor/quizzes/", "/instructor/assignments/",
            "/instructor/pools/",
            "/manage/roster/", "/manage/people/", "/manage/accommodations/",
            "/manage/integrity/", "/manage/activity/", "/manage/question-bank/",
            "/accounts/me/",
        ]
        for route in routes:
            response = anon.get(route)
            self.assertIn(response.status_code, (302, 301), route)
            self.assertIn("/accounts/login/", response["Location"], route)

    def test_public_routes_need_no_login(self):
        anon = Client()
        for route in ("/healthz/", "/readyz/", "/legal/terms/", "/legal/privacy/",
                      "/accounts/login/", "/accounts/password_reset/"):
            self.assertEqual(anon.get(route).status_code, 200, route)


class DashboardViewTests(ViewFixture):
    def test_dashboard_renders_with_context(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["organization"], self.org)
        self.assertEqual(len(response.context["assignments"]), 1)

    def test_dashboard_without_membership_is_403(self):
        orphan = User.objects.create_user("orphan", "o@t.dev", "pass-phrase-12")
        client = Client()
        client.force_login(orphan)
        self.assertEqual(client.get("/").status_code, 403)

    def test_dashboard_shows_the_live_daily_challenge(self):
        DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Today",
        )
        self.assertContains(self.client.get("/"), "Today")


class OrganizationSwitchTests(ViewFixture):
    def test_switching_between_two_memberships(self):
        second = Organization.objects.create(name="Second", slug="second")
        OrganizationMembership.objects.create(
            user=self.student, organization=second,
            role=OrganizationMembership.Role.STUDENT,
        )
        response = self.client.post(
            "/organization/switch/", {"organization_id": second.id}, follow=True
        )
        self.assertEqual(response.context["organization"], second)

    def test_cannot_switch_to_an_organization_you_do_not_belong_to(self):
        foreign = Organization.objects.create(name="Foreign", slug="foreign")
        response = self.client.post(
            "/organization/switch/", {"organization_id": foreign.id}
        )
        self.assertEqual(response.status_code, 403)

    def test_non_numeric_organization_id_is_400(self):
        response = self.client.post("/organization/switch/", {"organization_id": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_switch_requires_post(self):
        self.assertEqual(self.client.get("/organization/switch/").status_code, 405)


class StartAssignmentViewTests(ViewFixture):
    def test_start_redirects_to_the_attempt(self):
        response = self.client.post(f"/assignments/{self.assignment.id}/start/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/attempts/", response["Location"])

    def test_start_surfaces_permission_errors_as_messages(self):
        self.client.post(f"/assignments/{self.assignment.id}/start/")
        attempt = Attempt.objects.get()
        finalize_attempt(attempt, actor=self.student)
        response = self.client.post(
            f"/assignments/{self.assignment.id}/start/", follow=True
        )
        self.assertContains(response, "Attempt limit reached")

    def test_start_surfaces_authoring_errors_without_blaming_the_student(self):
        from assessments.models import ItemPool

        pool = ItemPool.objects.create(
            organization=self.org, name="Empty", required_question_count=3,
            created_by=self.instructor,
        )
        broken = QuizAssignment.objects.create(
            organization=self.org, title="Broken", form_mode="sampled", item_pool=pool,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        response = self.client.post(f"/assignments/{broken.id}/start/", follow=True)
        self.assertContains(response, "no approved, reusable questions")

    def test_start_is_rate_limited(self):
        for _ in range(12):
            self.client.post(f"/assignments/{self.assignment.id}/start/")
        response = self.client.post(f"/assignments/{self.assignment.id}/start/")
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_unknown_assignment_is_404(self):
        self.assertEqual(self.client.post("/assignments/99999/start/").status_code, 404)


class AttemptDetailViewTests(ViewFixture):
    def test_renders_questions_and_cutoff(self):
        attempt = start_attempt(self.student, self.assignment)
        response = self.client.get(f"/attempts/{attempt.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["questions"]), 2)
        self.assertIsNotNone(response.context["cutoff"])

    def test_expired_attempt_is_finalized_and_redirected(self):
        attempt = start_attempt(self.student, self.assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        response = self.client.get(f"/attempts/{attempt.id}/", follow=True)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.EXPIRED)
        self.assertContains(response, "finalized because the deadline passed")

    def test_untimed_attempt_has_no_cutoff(self):
        version = build_version(self.org, self.instructor, minutes=None)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Untimed", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        response = self.client.get(f"/attempts/{attempt.id}/")
        self.assertIsNone(response.context["cutoff"])


class SaveAnswerViewTests(ViewFixture):
    def setUp(self):
        super().setUp()
        self.attempt = start_attempt(self.student, self.assignment)
        self.item = self.attempt.item_snapshots.first()
        self.url = f"/attempts/{self.attempt.id}/save/{self.item.id}/"

    def test_successful_save_returns_status_fragment(self):
        choice = self.item.choice_snapshots.first()
        response = self.client.post(self.url, {"choices": choice.id, "revision": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Saved")
        self.assertIn("HX-Trigger", response)

    def test_conflict_returns_409_with_current_revision(self):
        choice = self.item.choice_snapshots.first()
        self.client.post(self.url, {"choices": choice.id, "revision": "0"})
        response = self.client.post(self.url, {"choices": choice.id, "revision": "0"})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'data-revision="1"', status_code=409)

    def test_expired_returns_410(self):
        Attempt.objects.filter(id=self.attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        response = self.client.post(self.url, {"choices": [], "revision": "0"})
        self.assertEqual(response.status_code, 410)

    def test_invalid_choice_returns_400_fragment(self):
        response = self.client.post(self.url, {"choices": "999999", "revision": "0"})
        self.assertEqual(response.status_code, 400)

    def test_save_requires_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_save_is_rate_limited(self):
        for i in range(125):
            self.client.post(self.url, {"choices": [], "revision": str(i)})
        response = self.client.post(self.url, {"choices": [], "revision": "0"})
        self.assertEqual(response.status_code, 429)

    def test_immediate_feedback_is_returned_and_rendered(self):
        version = build_version(self.org, self.instructor, policy="immediate")
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Immediate", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        item = attempt.item_snapshots.first()
        correct = item.choice_snapshots.get(text="right")
        response = self.client.post(
            f"/attempts/{attempt.id}/save/{item.id}/",
            {"choices": correct.id, "revision": "0"},
        )
        self.assertContains(response, "Correct")


class SubmitAndResultTests(ViewFixture):
    def test_submit_finalizes_and_redirects(self):
        attempt = start_attempt(self.student, self.assignment)
        response = self.client.post(f"/attempts/{attempt.id}/submit/")
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, Attempt.Status.FINALIZED)
        self.assertIn("/result/", response["Location"])

    def test_submit_requires_post(self):
        attempt = start_attempt(self.student, self.assignment)
        self.assertEqual(self.client.get(f"/attempts/{attempt.id}/submit/").status_code, 405)

    def test_result_shows_score_and_key_when_released(self):
        attempt = start_attempt(self.student, self.assignment)
        item = attempt.item_snapshots.first()
        save_response(attempt, self.student, item.id,
                      [item.choice_snapshots.get(text="right").id], 0)
        finalize_attempt(attempt, actor=self.student)
        response = self.client.get(f"/attempts/{attempt.id}/result/")
        self.assertTrue(response.context["visibility"].show_score)
        self.assertTrue(response.context["visibility"].show_answer_key)

    def test_result_of_an_in_progress_attempt_shows_no_score(self):
        version = build_version(self.org, self.instructor, minutes=None)
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Open", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        response = self.client.get(f"/attempts/{attempt.id}/result/")
        self.assertFalse(response.context["visibility"].show_score)
        self.assertEqual(response.context["visibility"].reason, "in_progress")

    def test_manual_release_flow_end_to_end(self):
        version = build_version(self.org, self.instructor, policy="manual")
        assignment = QuizAssignment.objects.create(
            organization=self.org, title="Manual", quiz_version=version,
            opens_at=timezone.now() - timedelta(minutes=1), attempt_limit=1,
            created_by=self.instructor,
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        self.assertContains(
            self.client.get(f"/attempts/{attempt.id}/result/"), "not released yet"
        )

        instructor_client = Client()
        instructor_client.force_login(self.instructor)
        instructor_client.post(
            f"/instructor/assignments/{assignment.id}/release/", {"is_open": "1"}
        )
        self.assertTrue(ResultRelease.objects.get(assignment=assignment).is_open)
        response = self.client.get(f"/attempts/{attempt.id}/result/")
        self.assertTrue(response.context["visibility"].show_answer_key)

    def test_release_can_be_withdrawn(self):
        instructor_client = Client()
        instructor_client.force_login(self.instructor)
        instructor_client.post(
            f"/instructor/assignments/{self.assignment.id}/release/", {"is_open": "1"}
        )
        instructor_client.post(
            f"/instructor/assignments/{self.assignment.id}/release/", {"is_open": "0"}
        )
        self.assertFalse(ResultRelease.objects.get(assignment=self.assignment).is_open)


class DailyChallengeViewTests(ViewFixture):
    def test_daily_page_without_a_challenge(self):
        self.assertContains(self.client.get("/daily/"), "No challenge is live today")

    def test_start_and_complete_a_live_challenge(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Live one",
        )
        response = self.client.post(f"/daily/{challenge.id}/start/")
        self.assertEqual(response.status_code, 302)
        attempt = Attempt.objects.get(daily_challenge=challenge)
        with self.captureOnCommitCallbacks(execute=True):
            finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertContains(self.client.get("/daily/"), "Completed")

    def test_cannot_start_the_same_challenge_twice(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Once",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        response = self.client.post(f"/daily/{challenge.id}/start/", follow=True)
        self.assertContains(response, "already taken")

    def test_archived_challenge_cannot_be_started_live(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org) - timedelta(days=2), title="Old",
        )
        response = self.client.post(f"/daily/{challenge.id}/start/", follow=True)
        self.assertContains(response, "not live today")

    def test_practice_on_an_archived_challenge(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org) - timedelta(days=2), title="Old",
        )
        response = self.client.post(f"/daily/{challenge.id}/practice/", follow=True)
        self.assertContains(response, "does not affect your streak")

    def test_practice_refuses_todays_challenge(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="Live",
        )
        response = self.client.post(f"/daily/{challenge.id}/practice/", follow=True)
        self.assertContains(response, "live")


class NotificationViewTests(ViewFixture):
    def test_list_and_mark_read(self):
        Notification.objects.create(
            organization=self.org, user=self.student, title="Hello",
            body="Body", dedupe_key="a",
        )
        self.assertContains(self.client.get("/notifications/"), "Hello")
        self.client.post("/notifications/read/")
        self.assertIsNotNone(Notification.objects.get().read_at)

    def test_mark_read_requires_post(self):
        self.assertEqual(self.client.get("/notifications/read/").status_code, 405)

    def test_empty_state(self):
        self.assertContains(self.client.get("/notifications/"), "Nothing yet")


class ProgressViewTests(ViewFixture):
    def test_progress_metrics(self):
        attempt = start_attempt(self.student, self.assignment)
        for item in attempt.item_snapshots.all():
            save_response(
                attempt, self.student, item.id,
                [item.choice_snapshots.get(text="right").id],
                attempt.revision,
            )
            attempt.refresh_from_db()
        finalize_attempt(attempt, actor=self.student)
        response = self.client.get("/progress/")
        self.assertEqual(response.context["scored_count"], 1)
        self.assertEqual(response.context["best_percent"], 100)


class HealthEndpointTests(TestCase):
    def setUp(self):
        # Rate-limit buckets live in the process cache and would otherwise leak
        # between tests in this class.
        cache.clear()

    def test_healthz_is_json_ok(self):
        response = Client().get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {"ok": True})

    def test_readyz_reports_components(self):
        response = Client().get("/readyz/")
        payload = response.json()
        self.assertEqual(payload["database"], "ok")
        self.assertEqual(payload["cache"], "ok")
        self.assertIn("worker", payload)

    def test_readyz_fails_closed_when_a_worker_is_required(self):
        with self.settings(READYZ_REQUIRE_WORKER=True):
            response = Client().get("/readyz/")
        self.assertEqual(response.status_code, 503)

    def test_health_endpoints_are_rate_limited(self):
        client = Client()
        for _ in range(31):
            client.get("/readyz/")
        self.assertEqual(client.get("/readyz/").status_code, 429)


class LegalPageTests(TestCase):
    def test_pages_render_and_link_each_other(self):
        client = Client()
        self.assertContains(client.get("/legal/terms/"), "Terms of use")
        self.assertContains(client.get("/legal/privacy/"), "Privacy notice")

"""Phase 4: infrastructure coverage — tasks, tenancy, rate limits, logging, config."""

import logging
from datetime import timedelta
from io import StringIO

from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership, User
from assessments.middleware import (
    ContentSecurityPolicyMiddleware,
    OrganizationTimezoneMiddleware,
    RequestIDMiddleware,
)
from assessments.models import (
    Attempt,
    Choice,
    DailyChallenge,
    DailyParticipation,
    ExportArtifact,
    Notification,
    Question,
    Quiz,
    QuizAssignment,
    StreakCache,
)
from assessments.ratelimit import client_identity, hit, peek, rate_limit
from assessments.services import (
    finalize_attempt,
    publish_quiz,
    recompute_streak,
    start_attempt,
    start_daily_attempt,
    today_for_org,
)
from assessments.tasks import (
    cleanup_expired_exports,
    finalize_expired_attempts,
    recompute_streak_cache,
    recompute_streak_caches,
    send_daily_challenge_notifications,
    send_due_soon_notifications,
    send_streak_at_risk_notifications,
)
from assessments.tenancy import (
    get_active_membership,
    get_active_organization,
    memberships_for,
    primary_membership,
    require_active_organization,
    set_active_organization,
)
from config.logging_filters import JSONFormatter, RequestIDFilter, scrub


def build_version(org, instructor, minutes=30):
    quiz = Quiz.objects.create(
        organization=org, title="Q", created_by=instructor, time_limit_minutes=minutes
    )
    question = Question.objects.create(
        quiz=quiz, prompt="P", question_type="single", points=1
    )
    Choice.objects.create(question=question, text="a", is_correct=True, sort_order=1)
    Choice.objects.create(question=question, text="b", is_correct=False, sort_order=2)
    return publish_quiz(quiz, instructor)


class InfraFixture(TestCase):
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
        self.version = build_version(self.org, self.instructor)


class TenancyModuleTests(InfraFixture):
    def test_memberships_for_anonymous_is_empty(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(memberships_for(AnonymousUser()).count(), 0)

    def test_primary_membership_uses_role_precedence_not_alphabet(self):
        # "org_admin" sorts after "instructor" alphabetically but outranks it.
        second = Organization.objects.create(name="Alpha", slug="alpha")
        OrganizationMembership.objects.create(
            user=self.student, organization=second,
            role=OrganizationMembership.Role.ORG_ADMIN,
        )
        self.assertEqual(primary_membership(self.student).organization, second)

    def test_primary_membership_none_without_memberships(self):
        orphan = User.objects.create_user("orphan", "o@t.dev", "pass-phrase-12")
        self.assertIsNone(primary_membership(orphan))

    def test_active_organization_falls_back_when_session_is_stale(self):
        request = RequestFactory().get("/")
        request.user = self.student
        request.session = {"active_organization_id": 99999}
        membership = get_active_membership(request)
        self.assertEqual(membership.organization, self.org)
        # The stale id is cleared rather than kept.
        self.assertNotIn("active_organization_id", request.session)

    def test_set_active_organization_rejects_foreign_ids(self):
        request = RequestFactory().get("/")
        request.user = self.student
        request.session = {}
        foreign = Organization.objects.create(name="F", slug="f")
        self.assertFalse(set_active_organization(request, foreign.id))
        self.assertTrue(set_active_organization(request, self.org.id))

    def test_require_active_organization_raises_without_membership(self):
        from django.core.exceptions import PermissionDenied

        request = RequestFactory().get("/")
        request.user = User.objects.create_user("no", "no@t.dev", "pass-phrase-12")
        request.session = {}
        with self.assertRaises(PermissionDenied):
            require_active_organization(request)

    def test_get_active_organization_reuses_the_middleware_result(self):
        request = RequestFactory().get("/")
        request.user = self.student
        request.session = {}
        request.membership = None
        self.assertIsNotNone(get_active_organization(request))


class RateLimitModuleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_hit_counts_down_and_blocks(self):
        for _ in range(3):
            self.assertTrue(hit("scope", "id", 3, 60))
        self.assertFalse(hit("scope", "id", 3, 60))

    def test_peek_does_not_consume(self):
        self.assertTrue(peek("scope", "id", 1, 60))
        self.assertTrue(peek("scope", "id", 1, 60))
        self.assertTrue(hit("scope", "id", 1, 60))
        self.assertFalse(peek("scope", "id", 1, 60))

    def test_identities_are_isolated(self):
        self.assertTrue(hit("s", "a", 1, 60))
        self.assertTrue(hit("s", "b", 1, 60))

    def test_authenticated_identity_uses_the_user_id(self):
        request = RequestFactory().get("/")
        request.user = User.objects.create_user("u1", "u1@t.dev", "pass-phrase-12")
        self.assertEqual(client_identity(request), f"user:{request.user.pk}")

    def test_anonymous_identity_ignores_forwarded_header_by_default(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.2.3.4")
        request.user = AnonymousUser()
        self.assertNotIn("1.2.3.4", client_identity(request))

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_header_is_honoured_behind_a_trusted_proxy(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="1.2.3.4, 5.6.7.8")
        request.user = AnonymousUser()
        self.assertEqual(client_identity(request), "ip:1.2.3.4")

    def test_decorator_returns_429_with_retry_after(self):
        @rate_limit("dec", limit=1, window_seconds=60)
        def view(request):
            from django.http import HttpResponse

            return HttpResponse("ok")

        request = RequestFactory().get("/")
        request.user = User.objects.create_user("u2", "u2@t.dev", "pass-phrase-12")
        self.assertEqual(view(request).status_code, 200)
        blocked = view(request)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked["Retry-After"], "60")

    def test_cache_failure_fails_open(self):
        from unittest.mock import patch

        with patch("assessments.ratelimit.cache.add", side_effect=RuntimeError("down")):
            # An exam must not stop because Redis blinked.
            self.assertTrue(hit("s", "i", 1, 60))
        with patch("assessments.ratelimit.cache.get", side_effect=RuntimeError("down")):
            self.assertTrue(peek("s", "i", 1, 60))


class MiddlewareTests(InfraFixture):
    def test_request_id_is_generated_and_returned(self):
        response = Client().get("/healthz/")
        self.assertTrue(response["X-Request-ID"])

    def test_supplied_request_id_is_reused_when_sane(self):
        response = Client().get("/healthz/", HTTP_X_REQUEST_ID="abc-123")
        self.assertEqual(response["X-Request-ID"], "abc-123")

    def test_non_printable_request_id_is_replaced(self):
        response = Client().get("/healthz/", HTTP_X_REQUEST_ID="bad\x00id")
        self.assertNotIn("\x00", response["X-Request-ID"])

    def test_csp_and_hardening_headers(self):
        response = Client().get("/healthz/")
        self.assertIn("default-src 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("geolocation=()", response["Permissions-Policy"])

    def test_existing_header_is_not_overwritten(self):
        from django.http import HttpResponse

        def get_response(request):
            response = HttpResponse("x")
            response["Content-Security-Policy"] = "custom"
            return response

        middleware = ContentSecurityPolicyMiddleware(get_response)
        request = RequestFactory().get("/")
        self.assertEqual(middleware(request)["Content-Security-Policy"], "custom")

    def test_timezone_middleware_activates_the_org_zone(self):
        Organization.objects.filter(id=self.org.id).update(timezone="Asia/Tokyo")
        captured = {}

        def get_response(request):
            from django.http import HttpResponse

            captured["tz"] = timezone.get_current_timezone_name()
            captured["org"] = request.organization
            return HttpResponse("ok")

        middleware = OrganizationTimezoneMiddleware(get_response)
        request = RequestFactory().get("/")
        request.user = self.student
        request.session = {}
        middleware(request)
        self.assertEqual(captured["tz"], "Asia/Tokyo")
        self.assertEqual(captured["org"], self.org)

    def test_timezone_middleware_is_a_noop_for_anonymous(self):
        from django.contrib.auth.models import AnonymousUser
        from django.http import HttpResponse

        middleware = OrganizationTimezoneMiddleware(lambda r: HttpResponse("ok"))
        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        middleware(request)
        self.assertIsNone(request.organization)

    def test_request_id_middleware_unit(self):
        from django.http import HttpResponse

        middleware = RequestIDMiddleware(lambda r: HttpResponse("ok"))
        request = RequestFactory().get("/")
        response = middleware(request)
        self.assertEqual(len(request.request_id), 32)
        self.assertEqual(response["X-Request-ID"], request.request_id)


class LoggingTests(TestCase):
    def test_scrub_redacts_secrets_recursively(self):
        payload = {
            "password": "p", "token": "t", "sampled_seed": "s",
            "list": [{"cookie": "c"}], "safe": "keep",
        }
        result = scrub(payload)
        self.assertEqual(result["password"], "[redacted]")
        self.assertEqual(result["list"][0]["cookie"], "[redacted]")
        self.assertEqual(result["safe"], "keep")

    def test_filter_supplies_defaults(self):
        record = logging.LogRecord("n", logging.INFO, __file__, 1, "m", None, None)
        RequestIDFilter().filter(record)
        self.assertEqual(record.request_id, "-")
        self.assertIsNone(record.attempt_id)

    def test_json_formatter_emits_parsable_output(self):
        import json

        record = logging.LogRecord("n", logging.INFO, __file__, 1, "hello %s", ("x",), None)
        payload = json.loads(JSONFormatter().format(record))
        self.assertEqual(payload["message"], "hello x")
        self.assertEqual(payload["level"], "INFO")

    def test_json_formatter_includes_exceptions(self):
        import json
        import sys

        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "n", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
            )
        payload = json.loads(JSONFormatter().format(record))
        self.assertIn("ValueError", payload["exception"])


class TaskTests(InfraFixture):
    def _assignment(self, **kwargs):
        defaults = {
            "organization": self.org,
            "title": kwargs.pop("title", "Task assignment"),
            "quiz_version": self.version,
            "opens_at": timezone.now() - timedelta(days=1),
            "attempt_limit": 1,
            "created_by": self.instructor,
        }
        defaults.update(kwargs)
        return QuizAssignment.objects.create(**defaults)

    def test_sweep_returns_zero_when_nothing_is_expired(self):
        assignment = self._assignment()
        start_attempt(self.student, assignment)
        self.assertEqual(finalize_expired_attempts(), 0)

    def test_sweep_respects_the_batch_size(self):
        for i in range(3):
            assignment = self._assignment(title=f"batch{i}")
            attempt = start_attempt(self.student, assignment)
            Attempt.objects.filter(id=attempt.id).update(
                deadline_at=timezone.now() - timedelta(hours=1)
            )
        self.assertEqual(finalize_expired_attempts(batch_size=2), 2)
        self.assertEqual(finalize_expired_attempts(batch_size=10), 1)

    def test_sweep_skips_rows_that_raise(self):
        from unittest.mock import patch

        assignment = self._assignment(title="poison")
        attempt = start_attempt(self.student, assignment)
        Attempt.objects.filter(id=attempt.id).update(
            deadline_at=timezone.now() - timedelta(hours=1)
        )
        with patch("assessments.tasks.finalize_attempt", side_effect=RuntimeError("x")):
            # A single bad row must not abort the whole sweep.
            self.assertEqual(finalize_expired_attempts(), 0)
        self.assertEqual(finalize_expired_attempts(), 1)

    def test_recompute_streak_cache_for_unknown_ids(self):
        self.assertFalse(recompute_streak_cache(999999, 999999))

    def test_recompute_streak_cache_rebuilds(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="D",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        StreakCache.objects.all().delete()
        self.assertTrue(recompute_streak_cache(self.student.id, self.org.id))
        self.assertEqual(
            StreakCache.objects.get(student=self.student).current_streak, 1
        )

    def test_recompute_all_streak_caches(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="D",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertEqual(recompute_streak_caches(), 1)

    def test_due_soon_skips_students_who_already_finished(self):
        assignment = self._assignment(
            title="Due", due_at=timezone.now() + timedelta(hours=2)
        )
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        self.assertEqual(send_due_soon_notifications(), 0)

    def test_due_soon_ignores_assignments_outside_the_horizon(self):
        self._assignment(title="Far", due_at=timezone.now() + timedelta(days=30))
        self.assertEqual(send_due_soon_notifications(), 0)

    def test_due_soon_renders_the_deadline_in_org_time(self):
        Organization.objects.filter(id=self.org.id).update(timezone="Asia/Tokyo")
        self._assignment(title="Tokyo", due_at=timezone.now() + timedelta(hours=2))
        send_due_soon_notifications()
        note = Notification.objects.get(user=self.student)
        self.assertNotIn("UTC", note.body)

    def test_daily_notifications_skip_participants(self):
        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org), title="D",
        )
        attempt = start_daily_attempt(self.student, challenge)
        finalize_attempt(attempt, reason="submitted", actor=self.student)
        self.assertEqual(send_daily_challenge_notifications(), 0)

    def test_daily_notifications_noop_without_a_challenge(self):
        self.assertEqual(send_daily_challenge_notifications(), 0)

    def test_streak_at_risk_only_warns_those_with_a_live_streak(self):
        self.assertEqual(send_streak_at_risk_notifications(), 0)

        challenge = DailyChallenge.objects.create(
            organization=self.org, quiz_version=self.version,
            release_date=today_for_org(self.org) - timedelta(days=1), title="Y",
        )
        assignment = self._assignment(title="For participation")
        attempt = start_attempt(self.student, assignment)
        finalize_attempt(attempt, actor=self.student)
        DailyParticipation.objects.create(
            organization=self.org, student=self.student, challenge=challenge,
            attempt=attempt, credit_date=today_for_org(self.org) - timedelta(days=1),
            earned_credit=True,
        )
        recompute_streak(self.student, self.org)
        self.assertEqual(send_streak_at_risk_notifications(), 1)
        # Idempotent on a second run.
        self.assertEqual(send_streak_at_risk_notifications(), 0)

    def test_cleanup_leaves_unexpired_exports_alone(self):
        ExportArtifact.objects.create(
            organization=self.org, requested_by=self.instructor, name="fresh",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(cleanup_expired_exports(), 0)
        self.assertEqual(ExportArtifact.objects.count(), 1)

    def test_cleanup_removes_the_file_when_present(self):
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".csv")
        os.close(handle)
        ExportArtifact.objects.create(
            organization=self.org, requested_by=self.instructor, name="old",
            file_path=path, expires_at=timezone.now() - timedelta(days=1),
        )
        self.assertEqual(cleanup_expired_exports(), 1)
        self.assertFalse(os.path.exists(path))


class SeedCommandTests(TestCase):
    def test_seed_demo_is_idempotent(self):
        out = StringIO()
        call_command("seed_demo", stdout=out)
        self.assertIn("Seeded QuizOps demo", out.getvalue())
        first_users = User.objects.count()

        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(User.objects.count(), first_users)

    def test_seed_demo_creates_a_usable_login(self):
        call_command("seed_demo", stdout=StringIO())
        client = Client()
        response = client.post(
            "/accounts/login/",
            {"username": "student", "password": "quizops-demo-2026"},
        )
        self.assertEqual(response.status_code, 302)

    def test_seeded_daily_challenge_is_live_today(self):
        call_command("seed_demo", stdout=StringIO())
        org = Organization.objects.get(slug="demo-academy")
        self.assertTrue(
            DailyChallenge.objects.filter(
                organization=org, release_date=today_for_org(org)
            ).exists()
        )


class WSGIASGITests(TestCase):
    def test_wsgi_application_is_importable(self):
        from config.wsgi import application

        self.assertIsNotNone(application)

    def test_asgi_application_is_importable(self):
        from config.asgi import application

        self.assertIsNotNone(application)

    def test_celery_app_has_a_beat_schedule(self):
        from config.celery import app

        # A beat container with an empty schedule silently does nothing.
        self.assertIn("finalize-expired-attempts", app.conf.beat_schedule)
        self.assertGreaterEqual(len(app.conf.beat_schedule), 6)


class SettingsTests(TestCase):
    def test_env_helpers(self):
        import os

        from config.settings import env_bool, env_list

        os.environ["TEST_FLAG"] = "yes"
        self.assertTrue(env_bool("TEST_FLAG"))
        os.environ["TEST_FLAG"] = "0"
        self.assertFalse(env_bool("TEST_FLAG"))
        self.assertFalse(env_bool("TEST_MISSING_FLAG"))
        os.environ["TEST_LIST"] = "a, b ,,c"
        self.assertEqual(env_list("TEST_LIST"), ["a", "b", "c"])
        del os.environ["TEST_FLAG"], os.environ["TEST_LIST"]

    def test_env_file_loader_does_not_override_real_env(self):
        import os
        import tempfile
        from pathlib import Path

        from config.settings import load_env_file

        os.environ["ALREADY_SET"] = "real"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ALREADY_SET=file\nFRESH_KEY=from-file\n# comment\nbad line\n")
            load_env_file(path)
        self.assertEqual(os.environ["ALREADY_SET"], "real")
        self.assertEqual(os.environ["FRESH_KEY"], "from-file")
        del os.environ["ALREADY_SET"], os.environ["FRESH_KEY"]

    def test_missing_env_file_is_tolerated(self):
        from pathlib import Path

        from config.settings import load_env_file

        load_env_file(Path("does-not-exist.env"))

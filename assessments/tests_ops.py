"""Phase 5: feature flags, job failure parking, single-flight locks, slow queries."""

import logging
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase

from accounts.models import Organization, OrganizationMembership, User
from assessments.flags import FeatureFlag, invalidate, is_enabled
from assessments.models import JobFailure
from assessments.tasks import ReportingTask, single_flight
from config.logging_filters import SlowQueryFilter


class FeatureFlagTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = Organization.objects.create(name="Org", slug="org")
        self.other = Organization.objects.create(name="Other", slug="other")

    def test_unknown_flag_returns_the_default(self):
        self.assertFalse(is_enabled("never-created"))
        self.assertTrue(is_enabled("never-created", default=True))

    def test_globally_enabled_flag(self):
        FeatureFlag.objects.create(key="global-on", enabled=True)
        self.assertTrue(is_enabled("global-on"))
        self.assertTrue(is_enabled("global-on", self.org))

    def test_disabled_flag_is_off_everywhere(self):
        FeatureFlag.objects.create(key="off", enabled=False)
        self.assertFalse(is_enabled("off"))
        self.assertFalse(is_enabled("off", self.org))

    def test_per_organization_rollout(self):
        flag = FeatureFlag.objects.create(key="pilot", enabled=False)
        flag.enabled_organizations.add(self.org)
        self.assertTrue(is_enabled("pilot", self.org))
        self.assertFalse(is_enabled("pilot", self.other))
        # No organization context means the global setting applies.
        self.assertFalse(is_enabled("pilot"))

    def test_results_are_cached_then_invalidated(self):
        flag = FeatureFlag.objects.create(key="cached", enabled=False)
        self.assertFalse(is_enabled("cached", self.org))
        FeatureFlag.objects.filter(id=flag.id).update(enabled=True)
        # Still the cached value.
        self.assertFalse(is_enabled("cached", self.org))
        invalidate("cached")
        self.assertTrue(is_enabled("cached", self.org))

    def test_lookup_failure_falls_back_to_the_default(self):
        with patch(
            "assessments.flags.FeatureFlag.objects.filter", side_effect=RuntimeError("db down")
        ):
            # A flag must never be the reason an exam cannot run.
            self.assertTrue(is_enabled("anything", default=True))
            self.assertFalse(is_enabled("anything", default=False))

    def test_cache_failure_does_not_break_resolution(self):
        FeatureFlag.objects.create(key="resilient", enabled=True)
        with patch("assessments.flags.cache.get", side_effect=RuntimeError("cache down")):
            with patch("assessments.flags.cache.set", side_effect=RuntimeError("cache down")):
                self.assertTrue(is_enabled("resilient"))

    def test_invalidate_tolerates_a_cache_outage(self):
        with patch("assessments.flags.cache.delete", side_effect=RuntimeError("down")):
            invalidate("whatever")

    def test_str(self):
        flag = FeatureFlag.objects.create(key="named", enabled=True)
        self.assertEqual(str(flag), "named: on")
        flag.enabled = False
        self.assertEqual(str(flag), "named: off")

    def test_admin_save_invalidates(self):
        admin_user = User.objects.create_user(
            "op", "op@t.dev", "pass-phrase-12", is_staff=True, is_superuser=True
        )
        admin_user.is_platform_operator = True
        admin_user.save()
        flag = FeatureFlag.objects.create(key="admin-flag", enabled=False)
        self.assertFalse(is_enabled("admin-flag"))

        client = Client()
        client.force_login(admin_user)
        client.post(
            f"/admin/assessments/featureflag/{flag.id}/change/",
            {"key": "admin-flag", "description": "", "enabled": "on",
             "enabled_organizations": [], "_save": "Save"},
        )
        # The admin invalidates on save, so the change is visible immediately.
        self.assertTrue(is_enabled("admin-flag"))


class SingleFlightTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_second_caller_is_skipped_while_the_lock_is_held(self):
        calls = []

        @single_flight("demo-lock")
        def job():
            calls.append(1)
            # Re-entering while the lock is held must be refused.
            self.assertEqual(nested(), 0)
            return 1

        @single_flight("demo-lock")
        def nested():
            calls.append("nested")
            return 99

        self.assertEqual(job(), 1)
        self.assertEqual(calls, [1])

    def test_the_lock_is_released_afterwards(self):
        @single_flight("released-lock")
        def job():
            return 7

        self.assertEqual(job(), 7)
        self.assertEqual(job(), 7)

    def test_the_lock_is_released_even_when_the_job_raises(self):
        @single_flight("raising-lock")
        def job():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            job()

        @single_flight("raising-lock")
        def later():
            return "ran"

        self.assertEqual(later(), "ran")

    def test_a_cache_outage_lets_the_job_run(self):
        @single_flight("outage-lock")
        def job():
            return "ran anyway"

        with patch("assessments.tasks.cache.add", side_effect=RuntimeError("down")):
            with patch("assessments.tasks.cache.delete", side_effect=RuntimeError("down")):
                self.assertEqual(job(), "ran anyway")


class JobFailureTests(TestCase):
    def test_on_failure_parks_the_error(self):
        task = ReportingTask()
        task.name = "assessments.tasks.demo"
        task.on_failure(ValueError("kaboom"), "task-123", (), {}, "traceback text")

        failure = JobFailure.objects.get()
        self.assertEqual(failure.task_name, "assessments.tasks.demo")
        self.assertEqual(failure.task_id, "task-123")
        self.assertIn("ValueError", failure.error)
        self.assertIn("traceback text", failure.traceback)
        self.assertIsNone(failure.acknowledged_at)

    def test_long_errors_are_truncated_to_fit(self):
        task = ReportingTask()
        task.name = "assessments.tasks.long"
        task.on_failure(ValueError("x" * 2000), "t", (), {}, "y" * 9000)
        failure = JobFailure.objects.get()
        self.assertLessEqual(len(failure.error), 500)
        self.assertLessEqual(len(failure.traceback), 4000)

    def test_a_failure_to_record_does_not_mask_the_original(self):
        task = ReportingTask()
        task.name = "assessments.tasks.meta"
        with patch(
            "assessments.models.JobFailure.objects.create", side_effect=RuntimeError("db down")
        ):
            # Must not raise: the original failure is what matters.
            task.on_failure(ValueError("original"), "t", (), {}, "info")
        self.assertEqual(JobFailure.objects.count(), 0)

    def test_retry_policy_is_configured(self):
        self.assertTrue(ReportingTask.retry_backoff)
        self.assertTrue(ReportingTask.retry_jitter)
        self.assertEqual(ReportingTask.max_retries, 3)
        self.assertLessEqual(ReportingTask.retry_backoff_max, 300)

    def test_str(self):
        failure = JobFailure.objects.create(
            task_name="t", error="something went wrong"
        )
        self.assertIn("something went wrong", str(failure))

    def test_admin_lists_and_acknowledges(self):
        admin_user = User.objects.create_user(
            "op2", "op2@t.dev", "pass-phrase-12", is_staff=True, is_superuser=True
        )
        admin_user.is_platform_operator = True
        admin_user.save()
        failure = JobFailure.objects.create(task_name="t", error="e")

        client = Client()
        client.force_login(admin_user)
        self.assertEqual(client.get("/admin/assessments/jobfailure/").status_code, 200)
        client.post(
            "/admin/assessments/jobfailure/",
            {"action": "acknowledge", "_selected_action": [str(failure.id)]},
        )
        failure.refresh_from_db()
        self.assertIsNotNone(failure.acknowledged_at)

    def test_failures_cannot_be_created_through_the_admin(self):
        admin_user = User.objects.create_user(
            "op3", "op3@t.dev", "pass-phrase-12", is_staff=True, is_superuser=True
        )
        admin_user.is_platform_operator = True
        admin_user.save()
        client = Client()
        client.force_login(admin_user)
        self.assertEqual(
            client.get("/admin/assessments/jobfailure/add/").status_code, 403
        )


class SlowQueryFilterTests(TestCase):
    def _record(self, duration=None):
        record = logging.LogRecord("django.db.backends", logging.DEBUG, __file__, 1,
                                   "SELECT 1", None, None)
        if duration is not None:
            record.duration = duration
        return record

    def test_fast_queries_are_dropped(self):
        self.assertFalse(SlowQueryFilter(threshold_ms=500).filter(self._record(0.01)))

    def test_slow_queries_pass_and_are_annotated(self):
        record = self._record(1.25)
        self.assertTrue(SlowQueryFilter(threshold_ms=500).filter(record))
        self.assertEqual(record.slow_query_seconds, 1.25)

    def test_records_without_a_duration_pass_through(self):
        self.assertTrue(SlowQueryFilter().filter(self._record()))

    def test_threshold_is_configurable(self):
        record = self._record(0.05)
        self.assertTrue(SlowQueryFilter(threshold_ms=10).filter(record))
        self.assertFalse(SlowQueryFilter(threshold_ms=100).filter(self._record(0.05)))


class OrganizationScopedFlagTests(TestCase):
    def test_flag_is_visible_to_the_pilot_tenant_only(self):
        org = Organization.objects.create(name="Pilot", slug="pilot")
        other = Organization.objects.create(name="Rest", slug="rest")
        user = User.objects.create_user("u", "u@t.dev", "pass-phrase-12")
        OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationMembership.Role.STUDENT
        )
        flag = FeatureFlag.objects.create(key="staged", enabled=False)
        flag.enabled_organizations.add(org)
        cache.clear()
        self.assertTrue(is_enabled("staged", org))
        self.assertFalse(is_enabled("staged", other))

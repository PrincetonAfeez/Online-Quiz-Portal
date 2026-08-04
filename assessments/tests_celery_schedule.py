"""Celery beat schedule — all periodic tasks and timezone."""

from django.test import TestCase

from config.celery import app

EXPECTED_SCHEDULE = {
    "finalize-expired-attempts": "assessments.tasks.finalize_expired_attempts",
    "recompute-streak-caches": "assessments.tasks.recompute_streak_caches",
    "send-due-soon-notifications": "assessments.tasks.send_due_soon_notifications",
    "send-daily-challenge-notifications": "assessments.tasks.send_daily_challenge_notifications",
    "send-streak-at-risk-notifications": "assessments.tasks.send_streak_at_risk_notifications",
    "cleanup-expired-exports": "assessments.tasks.cleanup_expired_exports",
    "apply-retention-policies": "assessments.tasks.apply_retention_policies",
}


class CeleryBeatScheduleTests(TestCase):
    def test_beat_schedule_contains_all_seven_tasks(self):
        schedule = app.conf.beat_schedule
        self.assertEqual(len(schedule), 7)
        for name, task_path in EXPECTED_SCHEDULE.items():
            self.assertIn(name, schedule, msg=f"missing entry {name!r}")
            self.assertEqual(
                schedule[name]["task"],
                task_path,
                msg=f"wrong task string for {name!r}",
            )

    def test_each_entry_has_a_schedule(self):
        for name, entry in app.conf.beat_schedule.items():
            self.assertIn("schedule", entry, msg=f"{name} missing schedule")
            self.assertIsNotNone(entry["schedule"])

    def test_celery_timezone_is_utc(self):
        self.assertEqual(app.conf.timezone, "UTC")

"""Celery application and periodic beat schedule."""

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("quizops")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# A beat container was running with an empty schedule, so the expiry sweep never
# executed and abandoned attempts stayed in progress forever (finding 13).
app.conf.beat_schedule = {
    "finalize-expired-attempts": {
        "task": "assessments.tasks.finalize_expired_attempts",
        "schedule": crontab(minute="*"),
    },
    "recompute-streak-caches": {
        "task": "assessments.tasks.recompute_streak_caches",
        "schedule": crontab(minute=10),
    },
    "send-due-soon-notifications": {
        "task": "assessments.tasks.send_due_soon_notifications",
        "schedule": crontab(minute=0),
    },
    "send-daily-challenge-notifications": {
        "task": "assessments.tasks.send_daily_challenge_notifications",
        "schedule": crontab(minute=5),
    },
    "send-streak-at-risk-notifications": {
        "task": "assessments.tasks.send_streak_at_risk_notifications",
        "schedule": crontab(minute=15),
    },
    "cleanup-expired-exports": {
        "task": "assessments.tasks.cleanup_expired_exports",
        "schedule": crontab(minute=30, hour=3),
    },
    "apply-retention-policies": {
        "task": "assessments.tasks.apply_retention_policies",
        "schedule": crontab(minute=15, hour=4),
    },
}
app.conf.timezone = "UTC"

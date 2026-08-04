"""Background jobs.

All jobs are idempotent and safe to re-run: sweeps filter on source state and
lock rows, notifications carry a dedupe key.

Three operational properties every task here shares:

* **Retry with backoff** — transient database or broker faults retry with
  exponential backoff and jitter rather than hammering a struggling dependency.
* **Failure parking** — a task that exhausts its retries lands in
  ``on_failure``, which records a ``JobFailure`` row. That table is the
  dead-letter queue operators alert on; nothing fails silently.
* **Single-flight scheduling** — periodic tasks take a short cache lock, so two
  beat containers (or an overlapping run) cannot double-execute.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import Task, shared_task
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from accounts.models import Organization, OrganizationMembership
from assessments.models import (
    Attempt,
    DailyChallenge,
    DailyParticipation,
    ExportArtifact,
    IntegrityEvent,
    JobFailure,
    Notification,
    QuizAssignment,
    ResponseEvent,
    StreakCache,
)
from assessments.services import attempt_cutoff, finalize_attempt, recompute_streak, today_for_org

logger = logging.getLogger(__name__)

LOCK_TTL = 600


class ReportingTask(Task):
    """Parks terminal failures where an operator can see them."""

    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = 3

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        from assessments.models import JobFailure

        logger.error("task %s failed permanently", self.name, exc_info=exc)
        try:
            JobFailure.objects.create(
                task_name=self.name,
                task_id=str(task_id),
                error=f"{type(exc).__name__}: {exc}"[:500],
                traceback=str(einfo)[:4000],
            )
        except Exception:
            # Never let the failure recorder mask the original failure.
            logger.exception("could not record the job failure")


def single_flight(lock_name: str, ttl: int = LOCK_TTL):
    """Refuse to start when another instance of this job holds the lock."""

    def decorator(func):
        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = f"joblock:{lock_name}"
            try:
                acquired = cache.add(key, "1", ttl)
            except Exception:
                # A cache outage must not stop the sweep; the jobs are
                # idempotent, so a double run is safe if unlikely.
                logger.warning("job lock unavailable for %s", lock_name, exc_info=True)
                acquired = True
            if not acquired:
                logger.info("skipping %s: another run holds the lock", lock_name)
                return 0
            try:
                return func(*args, **kwargs)
            finally:
                try:
                    cache.delete(key)
                except Exception:
                    logger.debug("could not release the job lock", exc_info=True)

        return wrapper

    return decorator


@shared_task(base=ReportingTask)
@single_flight("finalize_expired_attempts")
def finalize_expired_attempts(batch_size: int = 500) -> int:
    """Finalize attempts past their effective cutoff.

    The cutoff honours ``grace_seconds`` and the assignment close, so this path
    agrees with the interactive one instead of finalizing a student mid-save
    (finding 12).
    """
    now = timezone.now()
    # Coarse DB filter (a superset: ignores grace, which only makes the real
    # cutoff later), then the exact grace-aware cutoff is checked in Python so
    # the query stays portable across Postgres and SQLite.
    candidates = (
        Attempt.objects.filter(status=Attempt.Status.IN_PROGRESS)
        .filter(Q(deadline_at__lt=now) | Q(assignment__closes_at__lt=now))
        .select_related("assignment")
        # Order by id, not deadline_at: NULL deadlines (window-closed attempts)
        # sort last on Postgres and would starve behind a full batch.
        .order_by("id")[:batch_size]
    )

    finalized = 0
    for attempt in list(candidates):
        cutoff = attempt_cutoff(attempt)
        if cutoff is None or cutoff >= now:
            continue
        try:
            finalize_attempt(attempt, reason="sweep")
            finalized += 1
        except Exception:
            logger.exception("expiry sweep failed", extra={"attempt_id": attempt.id})
    return finalized


@shared_task(base=ReportingTask)
def recompute_streak_cache(student_id: int, organization_id: int) -> bool:
    from accounts.models import User

    try:
        student = User.objects.get(id=student_id)
        organization = Organization.objects.get(id=organization_id)
    except (User.DoesNotExist, Organization.DoesNotExist):
        return False
    recompute_streak(student, organization)
    return True


@shared_task(base=ReportingTask)
@single_flight("recompute_streak_caches")
def recompute_streak_caches() -> int:
    """Rebuild every cache from DailyParticipation, the source of truth."""
    pairs = (
        DailyParticipation.objects.values_list("student_id", "organization_id")
        .distinct()
        .order_by()
    )
    count = 0
    for student_id, organization_id in pairs:
        if recompute_streak_cache(student_id, organization_id):
            count += 1
    return count


def _notify(organization, user, kind, title, body, url, dedupe_key) -> bool:
    _, created = Notification.objects.get_or_create(
        user=user,
        dedupe_key=dedupe_key,
        defaults={
            "organization": organization,
            "kind": kind,
            "title": title,
            "body": body,
            "url": url,
        },
    )
    return created


def _students_for(assignment: QuizAssignment):
    """Recipients for assignment notifications.

    Mirrors eligibility: cohort, course, org-wide students, plus anyone listed
    on ``explicit_students`` (audit F05).
    """
    from accounts.models import User

    if assignment.cohort_id:
        scoped = User.objects.filter(
            enrollments__cohort_id=assignment.cohort_id, enrollments__is_active=True
        )
    elif assignment.course_id:
        scoped = User.objects.filter(
            enrollments__course_id=assignment.course_id, enrollments__is_active=True
        )
    else:
        scoped = User.objects.filter(
            memberships__organization_id=assignment.organization_id,
            memberships__role=OrganizationMembership.Role.STUDENT,
            memberships__is_active=True,
        )
    explicit = assignment.explicit_students.all()
    return User.objects.filter(
        Q(pk__in=scoped.values("pk")) | Q(pk__in=explicit.values("pk"))
    ).distinct()


@shared_task(base=ReportingTask)
@single_flight("send_due_soon_notifications")
def send_due_soon_notifications(hours_ahead: int = 24) -> int:
    now = timezone.now()
    horizon = now + timedelta(hours=hours_ahead)
    sent = 0
    assignments = QuizAssignment.objects.filter(
        is_active=True, due_at__gt=now, due_at__lte=horizon
    ).select_related("organization")
    for assignment in assignments:
        key = f"due_soon:{assignment.id}:{assignment.due_at.date()}"
        # Render the deadline in the organization's calendar, matching every
        # other time the student is shown.
        local_due = assignment.due_at.astimezone(assignment.organization.zoneinfo)
        already_done = set(
            Attempt.objects.filter(
                assignment=assignment, status__in=Attempt.SCORED_STATUSES
            ).values_list("student_id", flat=True)
        )
        for student in _students_for(assignment):
            if student.id in already_done:
                continue
            sent += _notify(
                assignment.organization,
                student,
                Notification.Kind.DUE_SOON,
                f"Due soon: {assignment.title}",
                f"This assessment is due {local_due:%b %d, %I:%M %p %Z}.",
                "/",
                key,
            )
    return sent


@shared_task(base=ReportingTask)
@single_flight("send_daily_challenge_notifications")
def send_daily_challenge_notifications() -> int:
    sent = 0
    for organization in Organization.objects.all():
        today = today_for_org(organization)
        challenge = DailyChallenge.objects.filter(
            organization=organization, release_date=today, is_archived=False
        ).first()
        if not challenge:
            continue
        students = OrganizationMembership.objects.filter(
            organization=organization,
            role=OrganizationMembership.Role.STUDENT,
            is_active=True,
        ).select_related("user")
        for membership in students:
            if DailyParticipation.objects.filter(
                student=membership.user, challenge=challenge
            ).exists():
                continue
            sent += _notify(
                organization,
                membership.user,
                Notification.Kind.DAILY_LIVE,
                challenge.title,
                "Today's challenge is live.",
                "/daily/",
                f"daily_live:{challenge.id}",
            )
    return sent


@shared_task(base=ReportingTask)
@single_flight("send_streak_at_risk_notifications")
def send_streak_at_risk_notifications() -> int:
    sent = 0
    for organization in Organization.objects.all():
        today = today_for_org(organization)
        yesterday = today - timedelta(days=1)
        # Not named `cache`: that shadows the module-level cache import this
        # file uses for job locking.
        streaks = StreakCache.objects.filter(
            organization=organization, current_streak__gt=0
        ).select_related("student")
        for streak in streaks:
            already = DailyParticipation.objects.filter(
                organization=organization,
                student=streak.student,
                credit_date=today,
                earned_credit=True,
            ).exists()
            kept_yesterday = DailyParticipation.objects.filter(
                organization=organization,
                student=streak.student,
                credit_date=yesterday,
                earned_credit=True,
            ).exists()
            if already or not kept_yesterday:
                continue
            sent += _notify(
                organization,
                streak.student,
                Notification.Kind.STREAK_AT_RISK,
                f"Your {streak.current_streak}-day streak is at risk",
                "Finish today's challenge to keep it.",
                "/daily/",
                f"streak_risk:{organization.id}:{today}",
            )
    return sent


@shared_task(base=ReportingTask)
@single_flight("cleanup_expired_exports")
def cleanup_expired_exports() -> int:
    """Exports are sensitive; expired artifacts must not linger (ADR-0021)."""
    import os

    expired = ExportArtifact.objects.filter(expires_at__lt=timezone.now())
    removed = 0
    for artifact in expired:
        if artifact.file_path and os.path.exists(artifact.file_path):
            try:
                os.remove(artifact.file_path)
            except OSError:
                logger.warning("could not remove export %s", artifact.id)
                continue
        artifact.delete()
        removed += 1
    return removed


# Retention windows from docs/PRIVACY.md (audit F02).
RESPONSE_NETWORK_RETENTION = timedelta(days=365 * 2)
INTEGRITY_EVENT_RETENTION = timedelta(days=365 * 2)
JOB_FAILURE_RETENTION = timedelta(days=90)


@shared_task(base=ReportingTask)
@single_flight("apply_retention_policies")
def apply_retention_policies() -> dict:
    """Clear or delete records past their documented retention windows.

    Idempotent: re-running after a no-op window updates nothing. Network
    identifiers on ResponseEvent are cleared in place so revision history
    stays verifiable; IntegrityEvent and acknowledged JobFailure rows are
    deleted outright.
    """
    now = timezone.now()
    network_cutoff = now - RESPONSE_NETWORK_RETENTION
    integrity_cutoff = now - INTEGRITY_EVENT_RETENTION
    job_cutoff = now - JOB_FAILURE_RETENTION

    network_cleared = ResponseEvent.objects.filter(
        created_at__lt=network_cutoff
    ).exclude(ip_address__isnull=True, user_agent="").update(
        ip_address=None, user_agent=""
    )
    integrity_deleted, _ = IntegrityEvent.objects.filter(
        created_at__lt=integrity_cutoff
    ).delete()
    # JobFailure is operational, not a business record — drop anything past 90 days.
    jobs_deleted, _ = JobFailure.objects.filter(created_at__lt=job_cutoff).delete()
    return {
        "response_network_cleared": network_cleared,
        "integrity_events_deleted": integrity_deleted,
        "job_failures_deleted": jobs_deleted,
    }

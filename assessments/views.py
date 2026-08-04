"""Student-facing views: dashboard, attempts, results, and health probes."""

from __future__ import annotations

import logging
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models import Count
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from assessments.models import (
    Attempt,
    DailyChallenge,
    DailyParticipation,
    Notification,
    QuizAssignment,
    StreakCache,
)
from assessments.ratelimit import rate_limit
from assessments.services import (
    AttemptConflict,
    AttemptExpired,
    archived_daily_challenges,
    assert_instructor,
    attempt_cutoff,
    current_daily_challenge,
    effective_attempt_limit,
    effective_pass_mark,
    mark_answer_key_disclosed,
    maybe_finalize_expired,
    record_integrity_event,
    result_visibility,
    save_response,
    set_result_release,
    start_attempt,
    start_daily_attempt,
    start_practice_attempt,
    student_read_model,
    submit_attempt_for,
    visible_assignments_for,
)
from assessments.tenancy import get_active_organization, set_active_organization

logger = logging.getLogger(__name__)


def _no_membership(request):
    """One consistent answer for "you belong to no organization" (finding 40)."""
    return render(request, "assessments/no_membership.html", status=403)


@login_required
def dashboard(request):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)

    assignments = visible_assignments_for(request.user, organization)
    attempts = (
        Attempt.objects.for_user(request.user)
        .filter(student=request.user, organization=organization)
        .select_related("assignment")
        .order_by("-started_at")[:8]
    )
    streak = StreakCache.objects.filter(
        organization=organization, student=request.user
    ).first()
    return render(
        request,
        "assessments/dashboard.html",
        {
            "organization": organization,
            "membership": request.membership,
            "assignments": assignments,
            "attempts": attempts,
            "streak": streak,
            "challenge": current_daily_challenge(organization),
            "now": timezone.now(),
        },
    )


@login_required
@require_POST
def switch_organization(request):
    try:
        organization_id = int(request.POST.get("organization_id", ""))
    except ValueError:
        return HttpResponseBadRequest("Invalid organization")
    if not set_active_organization(request, organization_id):
        raise PermissionDenied("You do not belong to that organization.")
    return redirect("dashboard")


@login_required
def instructor_dashboard(request):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    # Authorize and query against the *same* organization; the two used to be
    # allowed to differ (finding 2).
    assert_instructor(request.user, organization)

    assignments = (
        QuizAssignment.objects.for_user(request.user)
        .filter(organization=organization)
        .select_related("quiz_version", "item_pool", "course", "cohort", "result_release")
        .annotate(attempt_total=Count("attempts"))
        .order_by("-opens_at")
    )
    attempts = (
        Attempt.objects.for_user(request.user)
        .filter(organization=organization)
        .select_related("student", "assignment")[:20]
    )
    return render(
        request,
        "assessments/instructor_dashboard.html",
        {
            "organization": organization,
            "membership": request.membership,
            "assignments": assignments,
            "attempts": attempts,
        },
    )


@login_required
@require_POST
def toggle_result_release(request, assignment_id: int):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    assignment = get_object_or_404(
        QuizAssignment.objects.for_user(request.user).filter(organization=organization),
        id=assignment_id,
    )
    is_open = request.POST.get("is_open") == "1"
    set_result_release(assignment, request.user, is_open)
    messages.success(
        request,
        f"Results {'released' if is_open else 'withheld'} for {assignment.title}.",
    )
    return redirect("instructor_dashboard")


@login_required
@require_POST
@rate_limit("attempt_start", limit=10, window_seconds=60, message="Too many attempt starts.")
def start_assignment(request, assignment_id: int):
    assignment = get_object_or_404(QuizAssignment.objects.for_user(request.user), id=assignment_id)
    try:
        attempt = start_attempt(request.user, assignment)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("dashboard")
    except ValidationError as exc:
        # An authoring defect (e.g. an undersized pool) must not read as the
        # student's fault, and it must be visible to operators.
        logger.error(
            "assignment %s could not generate a form: %s", assignment.id, "; ".join(exc.messages)
        )
        messages.error(request, "; ".join(exc.messages))
        return redirect("dashboard")
    return redirect("attempt_detail", attempt_id=attempt.id)


@login_required
def attempt_detail(request, attempt_id: int):
    attempt = get_object_or_404(
        Attempt.objects.for_user(request.user).select_related("assignment", "quiz_version"),
        id=attempt_id,
    )
    if attempt.student_id != request.user.id:
        # An instructor has a legitimate need to look at a live attempt, but
        # never through the answer form. Send them to the read-only view.
        if _may_view_others(request, attempt):
            return redirect("result_detail", attempt_id=attempt.id)
        raise PermissionDenied
    # A finalized or voided attempt must not render a live answer form
    # (finding 21).
    if attempt.status != Attempt.Status.IN_PROGRESS:
        return redirect("result_detail", attempt_id=attempt.id)
    if maybe_finalize_expired(attempt):
        messages.info(request, "The attempt was finalized because the deadline passed.")
        return redirect("result_detail", attempt_id=attempt.id)

    return render(
        request,
        "assessments/attempt_detail.html",
        {
            "attempt": attempt,
            "questions": student_read_model(attempt),
            "cutoff": attempt_cutoff(attempt),
            "now": timezone.now(),
        },
    )


@login_required
@require_POST
@rate_limit("answer_save", limit=120, window_seconds=60, message="Too many saves.")
def save_answer(request, attempt_id: int, item_id: int):
    attempt = get_object_or_404(Attempt.objects.for_user(request.user), id=attempt_id)
    raw_choice_ids = request.POST.getlist("choices")
    raw_revision = request.POST.get("revision")
    if raw_revision is None:
        # Defaulting a missing revision to 0 turned optimistic concurrency into
        # an opt-in: a client that simply omitted the field always won.
        return HttpResponseBadRequest("Missing revision")
    try:
        choice_ids = [int(choice_id) for choice_id in raw_choice_ids]
        revision = int(raw_revision)
    except ValueError:
        return HttpResponseBadRequest("Invalid choice payload")
    if revision < 0:
        return HttpResponseBadRequest("Invalid revision")

    try:
        outcome = save_response(
            attempt, request.user, item_id, choice_ids, revision, request=request
        )
    except AttemptConflict as exc:
        record_integrity_event(exc)
        return _save_status(request, "conflict", str(exc), attempt, status_code=409)
    except AttemptExpired as exc:
        record_integrity_event(exc)
        return _save_status(request, "expired", str(exc), attempt, status_code=410)
    except ValidationError as exc:
        return _save_status(request, "invalid", "; ".join(exc.messages), attempt, status_code=400)

    return _save_status(
        request,
        "saved",
        "Saved",
        attempt,
        revision=outcome.revision,
        saved_at=outcome.response.saved_at,
        feedback=outcome.feedback,
    )


def _save_status(
    request,
    status,
    message,
    attempt,
    status_code=200,
    revision=None,
    saved_at=None,
    feedback=None,
):
    """Always send the authoritative revision so the client can recover.

    The old code echoed a stale in-memory revision on conflict, so the page
    could never resynchronise without a manual reload (finding 22).
    """
    if revision is None:
        attempt.refresh_from_db(fields=["revision"])
        revision = attempt.revision
    response = render(
        request,
        "assessments/partials/save_status.html",
        {
            "status": status,
            "message": message,
            "revision": revision,
            "saved_at": saved_at,
            "feedback": feedback,
        },
    )
    response.status_code = status_code
    response["HX-Trigger"] = f'{{"save-status": {{"status": "{status}", "revision": {revision}}}}}'
    return response


@login_required
@require_POST
def submit_attempt(request, attempt_id: int):
    attempt = get_object_or_404(Attempt.objects.for_user(request.user), id=attempt_id)
    if attempt.student_id != request.user.id:
        raise PermissionDenied
    if attempt.status == Attempt.Status.VOIDED_BY_ADMIN:
        raise PermissionDenied("This attempt was voided.")
    submit_attempt_for(attempt, request.user)
    return redirect("result_detail", attempt_id=attempt.id)


@login_required
def result_detail(request, attempt_id: int):
    attempt = get_object_or_404(
        Attempt.objects.for_user(request.user).select_related(
            "assignment", "assignment__result_release", "quiz_version"
        ),
        id=attempt_id,
    )
    if attempt.student_id != request.user.id and not _may_view_others(request, attempt):
        raise PermissionDenied
    if attempt.status == Attempt.Status.IN_PROGRESS:
        maybe_finalize_expired(attempt)
        attempt.refresh_from_db()

    visibility = result_visibility(attempt, request.user)
    # Stamp disclosure when the student themselves is shown the key (R4-01).
    if (
        visibility.show_answer_key
        and attempt.student_id == request.user.id
        and not attempt.answer_key_disclosed
    ):
        mark_answer_key_disclosed(attempt)
    rows = []
    if visibility.show_answer_key:
        response_map = {
            response.item_snapshot_id: {c.id for c in response.selected_choices.all()}
            for response in attempt.responses.prefetch_related("selected_choices")
        }
        # Partition the prefetched choices in Python instead of issuing a
        # correctness query per item (finding 33).
        for item in attempt.item_snapshots.prefetch_related("choice_snapshots"):
            choices = list(item.choice_snapshots.all())
            selected_ids = response_map.get(item.id, set())
            correct_ids = {choice.id for choice in choices if choice.is_correct}
            rows.append(
                {
                    "item": item,
                    "choices": choices,
                    "selected_ids": selected_ids,
                    "correct": bool(correct_ids) and selected_ids == correct_ids,
                }
            )

    pass_mark = effective_pass_mark(attempt.assignment)
    return render(
        request,
        "assessments/result_detail.html",
        {
            "attempt": attempt,
            "visibility": visibility,
            "rows": rows,
            "pass_mark": pass_mark,
            "passed": pass_mark is not None and attempt.score_percent >= pass_mark,
            "attempt_limit": effective_attempt_limit(attempt.assignment),
        },
    )


def _may_view_others(request, attempt) -> bool:
    from assessments.services import is_instructor, is_platform_operator

    return is_platform_operator(request.user) or is_instructor(request.user, attempt.organization)


@login_required
def daily_challenge(request):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    challenge = current_daily_challenge(organization)
    participation = None
    if challenge:
        participation = DailyParticipation.objects.filter(
            student=request.user, challenge=challenge
        ).select_related("attempt").first()
    return render(
        request,
        "assessments/daily.html",
        {
            "organization": organization,
            "membership": request.membership,
            "challenge": challenge,
            "participation": participation,
            "archive": archived_daily_challenges(organization)[:10],
            "streak": StreakCache.objects.filter(
                organization=organization, student=request.user
            ).first(),
        },
    )


@login_required
@require_POST
@rate_limit("attempt_start", limit=10, window_seconds=60)
def start_daily_challenge(request, challenge_id: int):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    challenge = get_object_or_404(
        DailyChallenge.objects.for_user(request.user).filter(organization=organization),
        id=challenge_id,
    )
    try:
        attempt = start_daily_attempt(request.user, challenge)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("daily_challenge")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("daily_challenge")
    return redirect("attempt_detail", attempt_id=attempt.id)


WORKER_PROBE_KEY = "readyz:worker"
WORKER_PROBE_TTL = 10


def _probe_worker() -> str:
    try:
        from config.celery import app as celery_app

        replies = celery_app.control.ping(timeout=0.25)
        return "ok" if replies else "no workers responded"
    except Exception as exc:
        return f"error: {type(exc).__name__}"


def terms(request):
    return render(request, "legal/terms.html")


def privacy(request):
    return render(request, "legal/privacy.html")


@rate_limit("healthz", limit=120, window_seconds=60)
def healthz(request):
    return JsonResponse({"ok": True})


# Readiness touches the database, cache and broker, so it is the more
# expensive of the two probes and gets the tighter budget.
@rate_limit("readyz", limit=30, window_seconds=60)
def readyz(request):
    """Database, cache, and worker liveness (spec 15).

    The worker probe is cached: pinging a broker that is down costs the full
    connection timeout, and a readiness endpoint that blocks for seconds is
    itself an outage as far as a load balancer is concerned.
    """
    from django.conf import settings
    from django.core.cache import cache

    components = {}
    ok = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        components["database"] = "ok"
    except Exception as exc:
        ok = False
        components["database"] = f"error: {type(exc).__name__}"

    try:
        cache.set("readyz:probe", "1", 5)
        components["cache"] = "ok" if cache.get("readyz:probe") == "1" else "error: readback"
        ok = ok and components["cache"] == "ok"
    except Exception as exc:
        ok = False
        components["cache"] = f"error: {type(exc).__name__}"

    require_worker = getattr(settings, "READYZ_REQUIRE_WORKER", False)
    worker_status = cache.get(WORKER_PROBE_KEY) if components.get("cache") == "ok" else None
    if worker_status is None:
        worker_status = _probe_worker()
        if components.get("cache") == "ok":
            try:
                cache.set(WORKER_PROBE_KEY, worker_status, WORKER_PROBE_TTL)
            except Exception:
                # Caching the probe is an optimisation; readiness still answers.
                logger.debug("could not cache the worker probe", exc_info=True)
    components["worker"] = worker_status
    if worker_status != "ok":
        ok = ok and not require_worker

    return JsonResponse(components, status=200 if ok else 503)


@login_required
def notifications(request):
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    items = Notification.objects.filter(user=request.user).select_related("organization")[:50]
    return render(
        request,
        "assessments/notifications.html",
        {"organization": organization, "notifications": items},
    )


@login_required
@require_POST
def mark_notifications_read(request):
    Notification.objects.filter(user=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )
    return redirect("notifications")


@login_required
def my_progress(request):
    """A student's full history, not just the eight most recent attempts."""
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)

    attempts = (
        Attempt.objects.for_user(request.user)
        .filter(student=request.user, organization=organization)
        .select_related("assignment")
        .order_by("-started_at")
    )
    scored = [a for a in attempts if a.status in Attempt.SCORED_STATUSES]
    average = (
        sum((a.score_percent for a in scored), Decimal("0")) / len(scored)
        if scored
        else Decimal("0")
    )
    return render(
        request,
        "assessments/my_progress.html",
        {
            "organization": organization,
            "attempts": attempts[:100],
            "total": attempts.count(),
            "scored_count": len(scored),
            "average_percent": average.quantize(Decimal("0.1")),
            "best_percent": max((a.score_percent for a in scored), default=Decimal("0")),
            "streak": StreakCache.objects.filter(
                organization=organization, student=request.user
            ).first(),
        },
    )


@login_required
@require_POST
@rate_limit("attempt_start", limit=10, window_seconds=60)
def start_practice_challenge(request, challenge_id: int):
    """Archived challenges are practice: they never affect streaks (spec 3.7)."""
    organization = get_active_organization(request)
    if organization is None:
        return _no_membership(request)
    challenge = get_object_or_404(
        DailyChallenge.objects.for_user(request.user).filter(organization=organization),
        id=challenge_id,
    )
    try:
        attempt = start_practice_attempt(request.user, challenge)
    except PermissionDenied as exc:
        messages.error(request, str(exc))
        return redirect("daily_challenge")
    messages.info(request, "Practice mode — this attempt does not affect your streak.")
    return redirect("attempt_detail", attempt_id=attempt.id)

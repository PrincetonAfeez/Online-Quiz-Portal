"""Data export and right-to-erasure.

Erasure tombstones the subject rather than deleting assessment history: scores,
finalizations and audit rows are immutable assessment facts that an institution
is normally obliged to retain. Identifying fields are severed or hashed and the
account is detached, so the record survives as an anonymous fact (ADR-0023).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def export_user_data(user) -> dict:
    """Everything the product holds about one person, in portable JSON."""
    from assessments.models import (
        Attempt,
        AttemptAudit,
        DailyParticipation,
        Enrollment,
        Notification,
        Response,
        ResponseEvent,
        StreakCache,
        TimingAccommodation,
    )

    attempts = (
        Attempt.objects.filter(student=user)
        .select_related("assignment", "organization")
        .order_by("started_at")
    )
    return {
        "generated_at": timezone.now().isoformat(),
        "format_version": 1,
        "account": {
            "username": user.username,
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "date_joined": _plain(user.date_joined),
            "last_login": _plain(user.last_login),
            "is_active": user.is_active,
        },
        "organizations": [
            {
                "organization": m.organization.name,
                "role": m.get_role_display(),
                "is_active": m.is_active,
                "joined": _plain(m.created_at),
            }
            for m in user.memberships.select_related("organization")
        ],
        "enrollments": [
            {
                "course": e.course.name,
                "cohort": e.cohort.name if e.cohort else None,
                "is_active": e.is_active,
            }
            for e in Enrollment.objects.filter(student=user).select_related("course", "cohort")
        ],
        "accommodations": [
            {
                "multiplier": _plain(a.multiplier),
                "extra_minutes": a.extra_minutes,
                "reason": a.reason,
                "is_active": a.is_active,
            }
            for a in TimingAccommodation.objects.filter(student=user)
        ],
        "attempts": [
            {
                "id": a.id,
                "assignment": a.assignment.title,
                "organization": a.organization.name,
                "attempt_number": a.attempt_number,
                "status": a.status,
                "score": _plain(a.score),
                "max_score": _plain(a.max_score),
                "percent": _plain(a.score_percent),
                "is_late": a.is_late,
                "started_at": _plain(a.started_at),
                "submitted_at": _plain(a.submitted_at),
                "finalized_at": _plain(a.finalized_at),
                "responses": [
                    {
                        "question": r.item_snapshot.prompt,
                        "selected": [c.text for c in r.selected_choices.all()],
                        "saved_at": _plain(r.saved_at),
                    }
                    for r in Response.objects.filter(attempt=a)
                    .select_related("item_snapshot")
                    .prefetch_related("selected_choices")
                ],
                "save_history": [
                    {"at": _plain(e.created_at), "revision": e.revision_after}
                    for e in ResponseEvent.objects.filter(attempt=a)
                ],
            }
            for a in attempts
        ],
        "daily_participation": [
            {
                "challenge": p.challenge.title,
                "credit_date": _plain(p.credit_date),
                "earned_credit": p.earned_credit,
            }
            for p in DailyParticipation.objects.filter(student=user).select_related("challenge")
        ],
        "streaks": [
            {
                "organization": s.organization.name,
                "current": s.current_streak,
                "longest": s.longest_streak,
            }
            for s in StreakCache.objects.filter(student=user).select_related("organization")
        ],
        "notifications": [
            {"title": n.title, "body": n.body, "created_at": _plain(n.created_at)}
            for n in Notification.objects.filter(user=user)
        ],
        "audit_trail": [
            {"event": a.event_type, "at": _plain(a.created_at)}
            for a in AttemptAudit.objects.filter(attempt__student=user)
        ],
    }


def subject_tombstone(user) -> str:
    """Stable opaque id so anonymized records remain internally linkable."""
    digest = hashlib.sha256(f"quizops-subject-{user.pk}".encode()).hexdigest()
    return digest[:32]


@transaction.atomic
def anonymize_user(user, actor=None, reason: str = "subject request") -> str:
    """Erase the person, keep the assessment facts.

    Returns the tombstone id. The account is deactivated and unusable
    afterwards; scores and audit rows survive, detached from any identity.
    """
    from assessments.models import (
        AdminActionLog,
        Notification,
        ResponseEvent,
        TimingAccommodation,
    )

    tombstone = subject_tombstone(user)

    # Sever direct identifiers on the account itself. Assessment rows remain
    # FK'd to this anonymized user row (username erased-<hash>); there is no
    # separate subject table — the opaque id is the tombstone string.
    user.username = f"erased-{tombstone[:12]}"
    user.email = ""
    user.first_name = ""
    user.last_name = ""
    user.is_active = False
    user.is_staff = False
    user.is_superuser = False
    user.is_platform_operator = False
    user.mfa_enabled = False
    user.mfa_secret = ""
    user.set_unusable_password()
    user.save()

    # Network identifiers on the append-only event log are personal data; the
    # rows themselves stay, so revision history remains verifiable.
    ResponseEvent.objects.filter(attempt__student=user).update(ip_address=None, user_agent="")

    # Accommodation reasons may contain health-adjacent text (audit F08).
    TimingAccommodation.objects.filter(student=user).update(reason="")

    # Notification bodies are free text addressed to a person.
    Notification.objects.filter(user=user).delete()

    for membership in user.memberships.select_related("organization"):
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        AdminActionLog.objects.create(
            organization=membership.organization,
            actor=actor,
            action="user.anonymized",
            target_type="User",
            target_id=tombstone,
            detail={"reason": reason},
        )

    logger.info("user anonymized", extra={"user_id": user.pk})
    return tombstone

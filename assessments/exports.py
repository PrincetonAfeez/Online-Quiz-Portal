"""Gradebook exports.

Exports are sensitive: a gradebook is every student's grade in one file. They
are therefore tenant-scoped, expiring, download-audited, and never served from a
guessable public path (ADR-0021).
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import timedelta

from django.utils import timezone

from assessments.analytics import assignment_analytics, gradebook_rows
from assessments.models import AdminActionLog, ExportArtifact
from assessments.services import assert_instructor, effective_pass_mark

logger = logging.getLogger(__name__)

EXPORT_TTL = timedelta(hours=24)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection.

    A student can set their own display name; Excel would otherwise execute a
    leading =, +, - or @ when an instructor opens the gradebook.
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def gradebook_csv(assignment) -> str:
    pass_mark = effective_pass_mark(assignment)
    rows = gradebook_rows(assignment, pass_mark)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "username",
            "student",
            "attempts",
            "best_score",
            "best_percent",
            "passed",
            "late",
            "latest_status",
            "finalized_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _csv_safe(row.username),
                _csv_safe(row.student_name),
                row.attempts,
                row.best_score,
                row.best_percent,
                "" if row.passed is None else ("yes" if row.passed else "no"),
                "yes" if row.is_late else "no",
                row.latest_status,
                row.finalized_at.isoformat() if row.finalized_at else "",
            ]
        )
    return buffer.getvalue()


def item_analysis_csv(assignment) -> str:
    analytics = assignment_analytics(assignment, effective_pass_mark(assignment))
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["question", "responses", "correct", "difficulty", "discrimination", "flags"])
    for item in analytics.items:
        writer.writerow(
            [
                _csv_safe(item.prompt),
                item.responses,
                item.correct,
                item.difficulty,
                item.discrimination,
                "; ".join(item.flags),
            ]
        )
    return buffer.getvalue()


def record_export(assignment, actor, name: str, payload: str) -> ExportArtifact:
    """Register an export for audit and expiry.

    The payload is streamed to the requester directly; only the metadata is
    persisted, so a gradebook never sits on disk waiting to be found.
    """
    assert_instructor(actor, assignment.organization)
    artifact = ExportArtifact.objects.create(
        organization=assignment.organization,
        requested_by=actor,
        name=name,
        file_path="",
        expires_at=timezone.now() + EXPORT_TTL,
        downloaded_at=timezone.now(),
    )
    AdminActionLog.objects.create(
        organization=assignment.organization,
        actor=actor,
        action="export.gradebook",
        target_type="QuizAssignment",
        target_id=str(assignment.id),
        detail={"name": name, "bytes": len(payload), "artifact_id": artifact.id},
    )
    logger.info(
        "gradebook exported",
        extra={
            "user_id": getattr(actor, "id", None),
            "organization_id": assignment.organization_id,
        },
    )
    return artifact

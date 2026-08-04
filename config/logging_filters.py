"""Structured logging helpers with request correlation and secret scrubbing."""

from __future__ import annotations

import json
import logging

SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "csrfmiddlewaretoken",
    "authorization",
    "cookie",
    "is_correct",
    "correct_ids",
    "answer_key",
    "sampled_seed",
}


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            request = getattr(record, "request", None)
            record.request_id = getattr(request, "request_id", "-")
        for attribute in ("user_id", "organization_id", "attempt_id", "quiz_id"):
            if not hasattr(record, attribute):
                setattr(record, attribute, None)
        return True


def scrub(value):
    """Never emit answer keys, credentials or tokens to a log sink (spec 12)."""
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if key.lower() in SENSITIVE_KEYS else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", None),
            "organization_id": getattr(record, "organization_id", None),
            "attempt_id": getattr(record, "attempt_id", None),
            "quiz_id": getattr(record, "quiz_id", None),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(scrub(payload), default=str)


class SlowQueryFilter(logging.Filter):
    """Let only slow queries through, so the signal is not buried in noise.

    Django's ``django.db.backends`` logger emits every query at DEBUG with a
    ``duration`` attribute. Logging all of them is unusable in production; this
    keeps the ones worth looking at.
    """

    def __init__(self, threshold_ms: float = 500.0):
        super().__init__()
        self.threshold_seconds = threshold_ms / 1000.0

    def filter(self, record: logging.LogRecord) -> bool:
        duration = getattr(record, "duration", None)
        if duration is None:
            return True
        if duration >= self.threshold_seconds:
            record.slow_query_seconds = round(duration, 3)
            return True
        return False

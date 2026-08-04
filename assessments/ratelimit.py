"""Cache-backed fixed-window rate limiting (spec 12).

Deliberately dependency-free: it runs on the configured cache, which is Redis in
production and LocMem in development.

Fail-open is intentional. A cache outage must not lock students out of an exam;
it is logged instead, and the operational alert is the mitigation.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)


def _bucket_key(scope: str, identity: str, window_seconds: int) -> str:
    window = int(time.time() // window_seconds)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"ratelimit:{scope}:{digest}:{window}"


def client_identity(request) -> str:
    """Identify the caller.

    ``X-Forwarded-For`` is honoured only when a trusted proxy is configured.
    Reading it unconditionally let an attacker rotate the header to get an
    unlimited number of login attempts.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return f"user:{user.pk}"
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.META.get('REMOTE_ADDR', '')}"


def peek(scope: str, identity: str, limit: int, window_seconds: int) -> bool:
    """Report whether the caller is within budget, without consuming any."""
    try:
        return (cache.get(_bucket_key(scope, identity, window_seconds)) or 0) < limit
    except Exception:
        logger.warning("rate limit cache unavailable (peek)", exc_info=True)
        return True


def hit(scope: str, identity: str, limit: int, window_seconds: int) -> bool:
    """Consume one unit. Returns True when the caller is still within budget."""
    key = _bucket_key(scope, identity, window_seconds)
    try:
        added = cache.add(key, 1, window_seconds)
        count = 1 if added else cache.incr(key)
    except ValueError:
        # The key expired between add() and incr(); treat as a fresh window.
        cache.set(key, 1, window_seconds)
        count = 1
    except Exception:
        logger.warning("rate limit cache unavailable (hit)", exc_info=True)
        return True
    return count <= limit


def refund(scope: str, identity: str, window_seconds: int) -> None:
    """Return one consumed unit (audit R2-11 — successful login).

    Uses a single atomic ``decr`` only. A get-then-delete raced with concurrent
    ``hit`` calls and could wipe the whole bucket (audit R3-03).
    """
    key = _bucket_key(scope, identity, window_seconds)
    try:
        cache.decr(key)
    except ValueError:
        # Missing or non-numeric key — nothing to refund.
        return
    except Exception:
        logger.debug("rate limit refund skipped", exc_info=True)


def rate_limit(scope: str, limit: int, window_seconds: int, message: str = "Too many requests."):
    def decorator(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not hit(scope, client_identity(request), limit, window_seconds):
                response = HttpResponse(message, status=429)
                response["Retry-After"] = str(window_seconds)
                return response
            return view(request, *args, **kwargs)

        return wrapper

    return decorator

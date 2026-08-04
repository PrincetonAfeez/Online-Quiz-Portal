"""Feature flags — safe rollout without a redeploy.

Deliberately small: a database row per flag, cached, resolvable globally or per
organization. The point is to be able to turn a risky path off during an exam
window without shipping code, and to roll one on for a single tenant first.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import models

from accounts.models import Organization

logger = logging.getLogger(__name__)

CACHE_TTL = 30


class FeatureFlag(models.Model):
    """A named switch.

    ``enabled`` is the global default; ``enabled_organizations`` opts specific
    tenants in when the global default is off, which is how a staged rollout
    starts.
    """

    key = models.SlugField(unique=True)
    description = models.CharField(max_length=240, blank=True)
    enabled = models.BooleanField(default=False)
    enabled_organizations = models.ManyToManyField(
        Organization, blank=True, related_name="enabled_flags"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key}: {'on' if self.enabled else 'off'}"


def _cache_key(key: str, organization_id: int | None) -> str:
    return f"flag:{key}:{organization_id or 'global'}"


def is_enabled(key: str, organization=None, default: bool = False) -> bool:
    """Resolve a flag.

    Fails to ``default`` on any error: a flag lookup must never be the reason a
    student cannot sit an exam.
    """
    organization_id = getattr(organization, "id", None)
    cache_key = _cache_key(key, organization_id)
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        logger.debug("flag cache unavailable", exc_info=True)

    try:
        flag = FeatureFlag.objects.filter(key=key).first()
        if flag is None:
            # Deliberately not cached: the answer here is the *caller's* default,
            # and caching it would let the first caller's default decide the
            # answer for every later caller that passed a different one.
            return default
        if flag.enabled:
            value = True
        elif organization_id is not None:
            value = flag.enabled_organizations.filter(id=organization_id).exists()
        else:
            value = False
    except Exception:
        logger.warning("flag lookup failed for %s", key, exc_info=True)
        return default

    try:
        cache.set(cache_key, value, CACHE_TTL)
    except Exception:
        logger.debug("could not cache flag", exc_info=True)
    return value


def invalidate(key: str) -> None:
    """Drop cached values for a flag across every organization.

    Called on save; the short TTL is the backstop when this misses.
    """
    try:
        cache.delete(_cache_key(key, None))
        for organization_id in Organization.objects.values_list("id", flat=True):
            cache.delete(_cache_key(key, organization_id))
    except Exception:
        logger.debug("flag invalidation failed", exc_info=True)

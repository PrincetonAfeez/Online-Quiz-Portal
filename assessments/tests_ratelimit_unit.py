"""Focused unit tests for assessments.ratelimit."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings

from accounts.models import User
from assessments.ratelimit import (
    _bucket_key,
    client_identity,
    hit,
    peek,
    refund,
)

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tests-ratelimit-unit",
    }
}


@override_settings(CACHES=LOCMEM_CACHES)
class RateLimitUnitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_hit_allows_requests_under_limit(self):
        for _ in range(5):
            self.assertTrue(hit("api", "caller", limit=5, window_seconds=60))

    def test_hit_blocks_at_limit(self):
        for _ in range(3):
            self.assertTrue(hit("write", "caller", limit=3, window_seconds=60))
        self.assertFalse(hit("write", "caller", limit=3, window_seconds=60))

    def test_peek_reports_over_limit_without_consuming(self):
        self.assertTrue(peek("read", "peer", limit=1, window_seconds=60))
        self.assertTrue(hit("read", "peer", limit=1, window_seconds=60))
        self.assertFalse(peek("read", "peer", limit=1, window_seconds=60))
        self.assertFalse(hit("read", "peer", limit=1, window_seconds=60))

    def test_refund_returns_one_unit(self):
        scope, identity, window = "login", "ip:10.0.0.1", 120
        self.assertTrue(hit(scope, identity, limit=2, window_seconds=window))
        self.assertTrue(hit(scope, identity, limit=2, window_seconds=window))
        refund(scope, identity, window)
        self.assertTrue(hit(scope, identity, limit=2, window_seconds=window))

    def test_refund_on_missing_key_is_harmless(self):
        refund("missing", "nobody", 60)

    def test_authenticated_client_identity_uses_user_pk(self):
        user = User.objects.create_user("rluser", password="pass-phrase-12")
        request = self.factory.get("/")
        request.user = user
        self.assertEqual(client_identity(request), f"user:{user.pk}")

    def test_anonymous_identity_uses_remote_addr_by_default(self):
        request = self.factory.get("/", REMOTE_ADDR="192.0.2.10")
        request.user = AnonymousUser()
        self.assertEqual(client_identity(request), "ip:192.0.2.10")

    def test_forwarded_header_ignored_without_trust_setting(self):
        request = self.factory.get("/", HTTP_X_FORWARDED_FOR="198.51.100.2")
        request.user = AnonymousUser()
        identity = client_identity(request)
        self.assertNotIn("198.51.100.2", identity)

    @override_settings(TRUST_X_FORWARDED_FOR=True)
    def test_forwarded_header_used_when_trusted(self):
        request = self.factory.get(
            "/",
            HTTP_X_FORWARDED_FOR="198.51.100.2, 203.0.113.5",
            REMOTE_ADDR="10.0.0.1",
        )
        request.user = AnonymousUser()
        self.assertEqual(client_identity(request), "ip:198.51.100.2")

    def test_hit_fails_open_when_cache_add_raises(self):
        with patch("assessments.ratelimit.cache.add", side_effect=RuntimeError("down")):
            self.assertTrue(hit("failopen", "id", limit=1, window_seconds=60))

    def test_peek_fails_open_when_cache_get_raises(self):
        with patch("assessments.ratelimit.cache.get", side_effect=RuntimeError("down")):
            self.assertTrue(peek("failopen", "id", limit=1, window_seconds=60))

    def test_refund_fails_open_when_cache_decr_raises(self):
        scope, identity, window = "refund-open", "id", 60
        self.assertTrue(hit(scope, identity, limit=5, window_seconds=window))
        with patch("assessments.ratelimit.cache.decr", side_effect=RuntimeError("down")):
            refund(scope, identity, window)

    def test_incr_value_error_starts_fresh_window(self):
        scope, identity, window = "race", "id", 60
        key = _bucket_key(scope, identity, window)
        cache.set(key, 1, window)
        with patch("assessments.ratelimit.cache.incr", side_effect=ValueError("expired")):
            self.assertTrue(hit(scope, identity, limit=2, window_seconds=window))
        self.assertEqual(cache.get(key), 1)

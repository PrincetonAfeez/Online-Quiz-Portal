"""Logging filters — scrub, request IDs, JSON formatting, slow queries."""

import json
import logging
import sys

from django.test import TestCase

from config.logging_filters import (
    JSONFormatter,
    RequestIDFilter,
    SlowQueryFilter,
    scrub,
)


class ScrubTests(TestCase):
    def test_scrubs_case_insensitive_sensitive_keys(self):
        payload = {
            "PASSWORD": "secret1",
            "Token": "secret2",
            "CsrfMiddlewareToken": "secret3",
            "Authorization": "Bearer x",
            "nested": {"Answer_Key": [1, 2], "Is_Correct": True},
        }
        result = scrub(payload)
        self.assertEqual(result["PASSWORD"], "[redacted]")
        self.assertEqual(result["Token"], "[redacted]")
        self.assertEqual(result["CsrfMiddlewareToken"], "[redacted]")
        self.assertEqual(result["Authorization"], "[redacted]")
        self.assertEqual(result["nested"]["Answer_Key"], "[redacted]")
        self.assertEqual(result["nested"]["Is_Correct"], "[redacted]")

    def test_scrubs_nested_lists_and_dicts(self):
        payload = {
            "items": [
                {"password": "p"},
                [{"cookie": "c"}, "plain"],
            ],
            "safe": "visible",
        }
        result = scrub(payload)
        self.assertEqual(result["items"][0]["password"], "[redacted]")
        self.assertEqual(result["items"][1][0]["cookie"], "[redacted]")
        self.assertEqual(result["items"][1][1], "plain")
        self.assertEqual(result["safe"], "visible")

    def test_non_sensitive_scalars_pass_through(self):
        self.assertEqual(scrub("hello"), "hello")
        self.assertEqual(scrub(42), 42)
        self.assertIsNone(scrub(None))


class RequestIDFilterTests(TestCase):
    def test_defaults_when_attributes_missing(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
        self.assertTrue(RequestIDFilter().filter(record))
        self.assertEqual(record.request_id, "-")
        self.assertIsNone(record.user_id)
        self.assertIsNone(record.organization_id)
        self.assertIsNone(record.attempt_id)
        self.assertIsNone(record.quiz_id)

    def test_reads_request_id_from_attached_request(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)

        class FakeRequest:
            request_id = "abc-123"

        record.request = FakeRequest()
        RequestIDFilter().filter(record)
        self.assertEqual(record.request_id, "abc-123")

    def test_preserves_existing_request_id(self):
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
        record.request_id = "preset"
        RequestIDFilter().filter(record)
        self.assertEqual(record.request_id, "preset")


class JSONFormatterTests(TestCase):
    def test_format_without_exc_info_omits_exception(self):
        record = logging.LogRecord(
            "app", logging.INFO, __file__, 10, "hello %s", ("world",), None
        )
        payload = json.loads(JSONFormatter().format(record))
        self.assertEqual(payload["message"], "hello world")
        self.assertEqual(payload["level"], "INFO")
        self.assertNotIn("exception", payload)

    def test_format_with_exc_info_includes_exception(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord(
                "app", logging.ERROR, __file__, 20, "failed", None, sys.exc_info()
            )
        payload = json.loads(JSONFormatter().format(record))
        self.assertIn("exception", payload)
        self.assertIn("ValueError", payload["exception"])
        self.assertIn("boom", payload["exception"])

    def test_format_scrubs_sensitive_extra_fields(self):
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "x", None, None)
        record.password = "hunter2"
        payload = json.loads(JSONFormatter().format(record))
        dumped = json.dumps(payload)
        self.assertNotIn("hunter2", dumped)


class SlowQueryFilterTests(TestCase):
    def _record(self, duration=None):
        record = logging.LogRecord("db", logging.DEBUG, __file__, 1, "query", None, None)
        if duration is not None:
            record.duration = duration
        return record

    def test_missing_duration_passes_through(self):
        self.assertTrue(SlowQueryFilter(threshold_ms=500).filter(self._record()))

    def test_fast_query_is_filtered_out(self):
        self.assertFalse(SlowQueryFilter(threshold_ms=500).filter(self._record(0.01)))

    def test_slow_query_passes_and_annotates_seconds(self):
        record = self._record(0.75)
        filt = SlowQueryFilter(threshold_ms=500)
        self.assertTrue(filt.filter(record))
        self.assertEqual(record.slow_query_seconds, 0.75)

    def test_threshold_boundary(self):
        self.assertTrue(SlowQueryFilter(threshold_ms=100).filter(self._record(0.10)))
        self.assertFalse(SlowQueryFilter(threshold_ms=100).filter(self._record(0.05)))

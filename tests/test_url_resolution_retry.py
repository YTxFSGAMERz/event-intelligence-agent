import unittest
from datetime import datetime, timedelta, timezone

from main import (
    MAX_URL_RESOLUTION_ATTEMPTS,
    URL_RESOLUTION_RETRY_MAX_SECONDS,
    url_resolution_retry_delay_seconds,
    url_resolution_retry_is_due,
)


class URLResolutionRetryTests(unittest.TestCase):
    def test_retry_delay_grows_exponentially_and_is_capped(self):
        self.assertEqual(url_resolution_retry_delay_seconds(0), 0)
        self.assertEqual(url_resolution_retry_delay_seconds(1), 6 * 60 * 60)
        self.assertEqual(url_resolution_retry_delay_seconds(2), 12 * 60 * 60)
        self.assertEqual(url_resolution_retry_delay_seconds(3), 24 * 60 * 60)
        self.assertEqual(url_resolution_retry_delay_seconds(99), URL_RESOLUTION_RETRY_MAX_SECONDS)

    def test_first_attempt_is_due_without_waiting(self):
        self.assertTrue(url_resolution_retry_is_due({"resolution_attempts": 0}))
        self.assertTrue(url_resolution_retry_is_due({}))

    def test_attempt_is_not_retried_before_backoff_has_elapsed(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        record = {
            "resolution_attempts": 1,
            "last_resolution_attempt_at": (now - timedelta(hours=5, minutes=59)).isoformat(),
        }
        self.assertFalse(url_resolution_retry_is_due(record, now=now))
        record["last_resolution_attempt_at"] = (now - timedelta(hours=6)).isoformat()
        self.assertTrue(url_resolution_retry_is_due(record, now=now))

    def test_second_attempt_uses_twelve_hour_delay(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        record = {
            "resolution_attempts": 2,
            "last_resolution_attempt_at": (now - timedelta(hours=11, minutes=59)).isoformat(),
        }
        self.assertFalse(url_resolution_retry_is_due(record, now=now))
        record["last_resolution_attempt_at"] = (now - timedelta(hours=12)).isoformat()
        self.assertTrue(url_resolution_retry_is_due(record, now=now))

    def test_naive_timestamps_are_interpreted_as_utc(self):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        record = {
            "resolution_attempts": 1,
            "last_resolution_attempt_at": (now - timedelta(hours=6)).replace(tzinfo=None).isoformat(),
        }
        self.assertTrue(url_resolution_retry_is_due(record, now=now))

    def test_invalid_legacy_attempt_count_does_not_crash_retry_check(self):
        self.assertTrue(url_resolution_retry_is_due({
            "resolution_attempts": "legacy-value",
            "last_resolution_attempt_at": "not-a-date",
        }))

    def test_malformed_timestamp_is_retried_but_exhausted_records_stop(self):
        self.assertTrue(url_resolution_retry_is_due({
            "resolution_attempts": 3,
            "last_resolution_attempt_at": "not-a-date",
        }))
        self.assertFalse(url_resolution_retry_is_due({
            "resolution_attempts": MAX_URL_RESOLUTION_ATTEMPTS,
            "last_resolution_attempt_at": "not-a-date",
        }))


if __name__ == "__main__":
    unittest.main()

import unittest

from main import UNKNOWN_DEADLINE_TEXT, normalize_event_record, normalize_state


class OpportunitySchemaTests(unittest.TestCase):
    def test_legacy_google_news_record_keeps_unknowns_unknown(self):
        event = normalize_event_record("abc123", {
            "title": "Example scholarship",
            "url": "https://news.google.com/rss/articles/example",
            "source": "https://news.google.com/rss/search?q=scholarship",
            "deadline": UNKNOWN_DEADLINE_TEXT,
            "registration_status": "UNKNOWN",
        })

        self.assertEqual(event["schema_version"], 1)
        self.assertIsNone(event["canonical_url"])
        self.assertEqual(event["discovered_url"], "https://news.google.com/rss/articles/example")
        self.assertIsNone(event["application_deadline"])
        self.assertIsNone(event["application_deadline_raw"])
        self.assertEqual(event["deadline_status"], "unknown")
        self.assertEqual(event["location"]["mode"], "unknown")
        self.assertEqual(event["travel_support"]["status"], "unknown")
        self.assertEqual(event["verification"]["status"], "unverified")

    def test_direct_url_is_not_mislabeled_as_verified(self):
        event = normalize_event_record("direct1", {
            "title": "Example hackathon",
            "url": "https://events.example.org/hackathon/",
            "deadline": "October 31, 2026",
        })

        self.assertEqual(event["canonical_url"], "https://events.example.org/hackathon")
        self.assertEqual(event["application_deadline"], None)
        self.assertEqual(event["application_deadline_raw"], "October 31, 2026")
        self.assertEqual(event["deadline_status"], "unverified")

    def test_legacy_keys_are_preserved_and_migration_is_idempotent(self):
        legacy = {
            "seen": {
                "event-1": {
                    "title": "Old record",
                    "url": "https://news.google.com/rss/articles/old",
                    "deadline": UNKNOWN_DEADLINE_TEXT,
                    "first_seen_utc": "2026-10-01T00:00:00+00:00",
                }
            }
        }
        once = normalize_state(legacy)
        twice = normalize_state(once)
        self.assertEqual(once, twice)
        self.assertEqual(once["seen"]["event-1"]["url"], legacy["seen"]["event-1"]["url"])
        self.assertEqual(once["seen"]["event-1"]["first_seen_utc"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(once["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()

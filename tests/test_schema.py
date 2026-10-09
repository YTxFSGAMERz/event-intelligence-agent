import unittest

from datetime import datetime, timezone

from main import (
    UNKNOWN_DEADLINE_TEXT,
    find_existing_event_id,
    normalize_event_record,
    normalize_state,
    normalize_title_key,
    remember_source_alias,
)


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

    def test_cross_source_exact_title_deduplication_is_conservative(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        seen = {
            "existing-event": {
                "title": "International Student Hackathon & Prize Challenge 2026!",
                "url": "https://publisher.example/articles/42",
                "first_seen_utc": "2026-10-01T00:00:00+00:00",
            },
            "old-event": {
                "title": "International Student Hackathon & Prize Challenge 2025!",
                "url": "https://publisher.example/articles/41",
                "first_seen_utc": "2025-01-01T00:00:00+00:00",
            },
        }

        candidate_title = "International student hackathon prize challenge 2026"
        self.assertEqual(
            normalize_title_key(candidate_title),
            "international student hackathon prize challenge 2026",
        )
        self.assertEqual(
            find_existing_event_id(
                seen,
                "https://another-index.example/events/42",
                candidate_title,
                now=now,
            ),
            "existing-event",
        )
        self.assertIsNone(
            find_existing_event_id(
                seen,
                "https://another-index.example/events/old",
                "International Student Hackathon & Prize Challenge 2025!",
                now=now,
            )
        )
        self.assertIsNone(
            find_existing_event_id(
                seen,
                "https://another-index.example/hackathon",
                "Hackathon 2026",
                now=now,
            )
        )

    def test_source_aliases_are_merged_without_duplicate_values(self):
        record = {
            "source_id": "source-a",
            "source_name": "Source A",
            "source_feed_url": "https://source-a.example/feed.xml",
            "discovered_url": "https://events.example/event",
        }
        remember_source_alias(
            record,
            "source-b",
            "Source B",
            "https://source-b.example/feed.xml",
            "https://events.example/event?utm_source=b",
            "https://events.example/event",
        )
        remember_source_alias(
            record,
            "source-b",
            "Source B",
            "https://source-b.example/feed.xml",
            "https://events.example/event?utm_source=b",
            "https://events.example/event",
        )

        self.assertEqual(record["source_ids"], ["source-a", "source-b"])
        self.assertEqual(record["source_names"], ["Source A", "Source B"])
        self.assertEqual(
            record["source_feeds"],
            ["https://source-a.example/feed.xml", "https://source-b.example/feed.xml"],
        )
        self.assertEqual(record["discovered_urls"], ["https://events.example/event"])
        self.assertEqual(record["resolved_urls"], ["https://events.example/event"])

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

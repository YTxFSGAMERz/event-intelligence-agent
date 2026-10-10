import unittest

from datetime import datetime, timezone

from main import (
    UNKNOWN_DEADLINE_TEXT,
    apply_page_verification,
    find_existing_event_id,
    _verification_target,
    normalize_event_record,
    normalize_unstop_display_title,
    normalize_state,
    normalize_title_key,
    legacy_resolution_priority,
    remember_source_alias,
    source_observed_facts,
)


class OpportunitySchemaTests(unittest.TestCase):
    def test_legacy_resolution_prioritizes_specific_opportunities_over_roundups(self):
        records = {
            "roundup": {
                "title": "Top scholarships in Europe",
                "resolution_attempts": 0,
                "first_seen_utc": "2026-10-01T00:00:00+00:00",
                "discovery_quality": {"accepted": False},
            },
            "specific": {
                "title": "Nebius Global AI Hackathon",
                "resolution_attempts": 1,
                "first_seen_utc": "2026-10-09T00:00:00+00:00",
                "discovery_quality": {"accepted": True},
            },
            "unknown": {
                "title": "Possible student opportunity",
                "resolution_attempts": 0,
                "first_seen_utc": "2026-10-02T00:00:00+00:00",
                "discovery_quality": {"accepted": None},
            },
        }
        ordered = sorted(records.items(), key=legacy_resolution_priority)
        self.assertEqual([key for key, _ in ordered], ["specific", "unknown", "roundup"])

    def test_unstop_legacy_title_cleanup_is_idempotent_and_preserves_summary(self):
        event = normalize_event_record("unstop-old", {
            "title": "Online Free HP Power Lab 3.0",
            "summary": "Unstop public platform listing. Listing text: Online Free HP Power Lab 3.0 11319 Registered 25 days left.",
            "url": "https://unstop.com/competitions/hp-power-lab-3-0-1759161",
            "adapter_type": "unstop_html",
        })

        self.assertEqual(event["title"], "HP Power Lab 3.0")
        self.assertIn("Online Free HP Power Lab 3.0", event["summary"])
        self.assertEqual(normalize_unstop_display_title(event["title"]), "HP Power Lab 3.0")

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

    def test_mlh_calendar_observations_remain_separate_from_verified_facts(self):
        observed = source_observed_facts({
            "adapter_type": "mlh_events_html",
            "source_name": "MLH · Upcoming Events Calendar",
            "source": "https://mlh.com/events",
            "entry": {
                "event_date_text": "OCT 09 - 11",
                "calendar_year": 2026,
                "location_text": "Chapel Hill, North Carolina, US In-Person",
            },
        })

        self.assertEqual(observed["schedule"]["raw"], "OCT 09 - 11")
        self.assertEqual(observed["schedule"]["calendar_year"], 2026)
        self.assertEqual(observed["schedule"]["confidence"], "observed_unverified")
        self.assertEqual(observed["location"]["raw"], "Chapel Hill, North Carolina, US In-Person")
        self.assertEqual(observed["location"]["mode"], "in_person")
        self.assertEqual(observed["location"]["country"], "United States")
        self.assertEqual(observed["location"]["confidence"], "observed_unverified")

        event = normalize_event_record("mlh-hacknc", {
            "title": "HackNC",
            "url": "https://hacknc.com",
            "source": "https://mlh.com/events",
            "adapter_type": "mlh_events_html",
            "source_observed": observed,
        })
        self.assertEqual(event["source_observed"], observed)
        self.assertEqual(event["location"]["mode"], "unknown")
        self.assertIsNone(event["event_start_at"])
        self.assertIsNone(event["event_end_at"])
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

    def test_official_page_facts_update_canonical_fields_with_evidence(self):
        record = {
            "title": "Example Student Hackathon Prize Challenge",
            "url": "https://publisher.example/article",
            "canonical_url": "https://publisher.example/article",
            "deadline": UNKNOWN_DEADLINE_TEXT,
            "application_deadline": None,
            "deadline_status": "unknown",
            "location": {"raw": None, "mode": "unknown", "city": None},
            "eligibility": {"status": "unknown", "requirements": []},
            "travel_support": {"status": "unknown", "flight": "unknown"},
            "verification": {"status": "unverified", "official_url": None, "evidence_urls": []},
        }
        official = "https://organizer.example/events/1"
        result = {
            "status": "official_page_verified",
            "verification_version": 1,
            "official_page_verified": True,
            "official_url": official,
            "source_page_url": "https://publisher.example/article",
            "official_link_reason": "outbound_anchor:Official Website",
            "last_checked_at": "2026-10-09T12:00:00+00:00",
            "evidence_urls": [official],
            "observed_facts": {},
            "fact_evidence": {
                "application_deadline_raw": {
                    "value": "October 31, 2026",
                    "source_url": official,
                    "method": "explicit_deadline_label",
                    "snippet": "Application deadline: October 31, 2026.",
                }
            },
            "verified_facts": {
                "organizer": "Example Foundation",
                "event_start_at": "2026-11-14T09:00:00+05:30",
                "event_end_at": "2026-11-15T18:00:00+05:30",
                "event_timezone": "UTC+05:30",
                "opportunity_status": "unknown",
                "location": {
                    "raw": "Pune, Maharashtra, India",
                    "mode": "in_person",
                    "venue": "Innovation Centre",
                    "city": "Pune",
                    "region": "Maharashtra",
                    "country": "India",
                    "country_code": "IN",
                    "remote_restrictions": [],
                },
                "deadline": {
                    "raw": "October 31, 2026",
                    "normalized": "2026-10-31",
                    "precision": "date",
                    "snippet": "Application deadline: October 31, 2026.",
                    "source_url": official,
                },
                "eligibility": {
                    "status": "found",
                    "requirements": ["Eligibility: Open to undergraduate students."],
                    "countries": [],
                    "education_levels": [],
                    "study_years": [],
                    "fields_of_study": [],
                    "age_min": None,
                    "age_max": None,
                    "evidence_url": official,
                },
                "travel_support": {
                    "status": "confirmed",
                    "flight": "confirmed",
                    "accommodation": "confirmed",
                    "meals": "not_offered",
                    "visa_support": "unknown",
                    "transport_reimbursement": "unknown",
                    "conditions": [],
                    "maximum_amount": None,
                    "currency": None,
                    "evidence_url": official,
                },
                "registration_status": "open",
                "registration_status_evidence": "Applications are open.",
                "image_url": None,
            },
        }

        updated = apply_page_verification(record, result)
        self.assertEqual(updated["url"], official)
        self.assertEqual(updated["verification"]["official_url"], official)
        self.assertEqual(updated["verification"]["status"], "official_page_verified")
        self.assertEqual(updated["verification"]["version"], 1)
        self.assertEqual(updated["application_deadline"], "2026-10-31")
        self.assertEqual(updated["application_deadline_raw"], "October 31, 2026")
        self.assertEqual(updated["deadline_status"], "verified")
        self.assertEqual(updated["location"]["city"], "Pune")
        self.assertEqual(updated["event_start_at"], "2026-11-14T09:00:00+05:30")
        self.assertEqual(updated["eligibility"]["status"], "verified")
        self.assertEqual(updated["travel_support"]["flight"], "confirmed")
        self.assertEqual(updated["registration_status"], "OPEN (official page)")

    def test_verified_remote_mode_is_saved_without_physical_address(self):
        record = {
            "title": "Online Student Hackathon",
            "location": {
                "raw": None, "mode": "unknown", "venue": None, "city": None,
                "region": None, "country": None, "country_code": None,
                "remote_restrictions": [],
            },
            "verification": {"status": "unverified", "official_url": None},
        }
        result = {
            "status": "official_page_verified",
            "verification_version": 1,
            "official_page_verified": True,
            "official_url": "https://organizer.example/event",
            "source_page_url": "https://organizer.example/event",
            "last_checked_at": "2026-10-09T12:00:00+00:00",
            "evidence_urls": ["https://organizer.example/event"],
            "fact_evidence": {},
            "observed_facts": {},
            "verified_facts": {
                "location": {
                    "raw": None, "mode": "remote", "venue": None, "city": None,
                    "region": None, "country": None, "country_code": None,
                    "remote_restrictions": [],
                },
            },
        }
        updated = apply_page_verification(record, result)
        self.assertEqual(updated["location"]["mode"], "remote")
        self.assertEqual(updated["verification"]["status"], "official_page_verified")

    def test_unofficial_discovery_page_facts_are_observations_not_verified_fields(self):
        record = {
            "title": "Example Student Scholarship Program",
            "url": "https://publisher.example/article",
            "application_deadline": None,
            "deadline_status": "unknown",
            "location": {"raw": None, "mode": "unknown", "city": None},
            "verification": {"status": "unverified", "official_url": None, "evidence_urls": []},
        }
        result = {
            "status": "source_page_checked",
            "verification_version": 1,
            "official_page_verified": False,
            "official_url": None,
            "source_page_url": "https://publisher.example/article",
            "last_checked_at": "2026-10-09T12:00:00+00:00",
            "evidence_urls": ["https://publisher.example/article"],
            "observed_facts": {
                "deadline": {"raw": "October 31, 2026", "normalized": "2026-10-31"},
                "location": {"mode": "remote", "city": None},
                "travel_support": {"status": "confirmed", "flight": "confirmed"},
            },
            "fact_evidence": {},
            "verified_facts": {},
        }
        updated = apply_page_verification(record, result)
        self.assertIsNone(updated["application_deadline"])
        self.assertEqual(updated["deadline_status"], "unknown")
        self.assertEqual(updated["location"]["mode"], "unknown")
        self.assertEqual(updated["verification"]["status"], "source_page_checked")
        self.assertEqual(
            updated["verification"]["observed_facts"]["travel_support"]["flight"],
            "confirmed",
        )
        self.assertIsNone(updated["verification"]["official_url"])

    def test_unresolved_google_news_wrapper_is_not_used_as_verification_target(self):
        self.assertIsNone(_verification_target({
            "url": "https://news.google.com/rss/articles/abc?oc=5",
            "discovered_url": "https://news.google.com/rss/articles/abc?oc=5",
            "verification": {"official_url": None},
        }))
        self.assertEqual(
            _verification_target({
                "url": "https://news.google.com/rss/articles/abc?oc=5",
                "canonical_url": "https://publisher.example/articles/42",
                "discovered_url": "https://news.google.com/rss/articles/abc?oc=5",
                "verification": {"official_url": None},
            }),
            "https://publisher.example/articles/42",
        )

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

import unittest

from main import refresh_existing_discovery_quality


class DiscoveryQualityBackfillTests(unittest.TestCase):
    def test_rechecks_legacy_roundup_under_current_rule_version(self):
        seen = {
            "legacy-roundup": {
                "title": "Top Fully Funded Scholarships in Ireland 2027",
                "summary": "A guide to several scholarship options.",
                "categories": ["scholarships_fellowships", "funded_travel_abroad"],
                "adapter_type": "google_news_rss",
                "discovery_quality": {
                    "accepted": True,
                    "status": "specific_opportunity",
                    "reason": "accepted by an earlier rule",
                    "rule_version": 1,
                },
            }
        }

        stats = refresh_existing_discovery_quality(seen)

        self.assertEqual(stats["rechecked"], 1)
        self.assertEqual(stats["flagged"], 1)
        self.assertFalse(seen["legacy-roundup"]["discovery_quality"]["accepted"])
        self.assertEqual(seen["legacy-roundup"]["discovery_quality"]["status"], "roundup_or_listicle")
        self.assertEqual(seen["legacy-roundup"]["discovery_quality"]["rule_version"], 2)
        self.assertIn("quality_checked_at_utc", seen["legacy-roundup"])

    def test_officially_verified_record_cannot_be_hidden_by_headline_heuristic(self):
        seen = {
            "verified-roundup": {
                "title": "Top Fully Funded Scholarships in Ireland 2027",
                "summary": "A guide to several scholarship options.",
                "categories": ["scholarships_fellowships"],
                "discovery_quality": {
                    "accepted": False,
                    "status": "roundup_or_listicle",
                    "reason": "earlier heuristic flagged it",
                    "rule_version": 1,
                },
                "verification": {
                    "status": "official_page_verified",
                    "official_url": "https://organizer.example/opportunity",
                },
            }
        }

        refresh_existing_discovery_quality(seen)

        quality = seen["verified-roundup"]["discovery_quality"]
        self.assertTrue(quality["accepted"])
        self.assertEqual(quality["status"], "official_source_verified")
        self.assertTrue(quality["official_verification_override"])

    def test_current_rule_assessment_is_idempotent(self):
        seen = {
            "already-current": {
                "title": "Example student hackathon",
                "summary": "Submit a project.",
                "categories": ["hackathons_buildathons"],
                "discovery_quality": {
                    "accepted": True,
                    "status": "specific_opportunity",
                    "reason": "current rule",
                    "rule_version": 2,
                },
                "quality_checked_at_utc": "2026-10-01T00:00:00+00:00",
            }
        }

        stats = refresh_existing_discovery_quality(seen)

        self.assertEqual(stats["rechecked"], 0)
        self.assertEqual(stats["already_current"], 1)
        self.assertEqual(
            seen["already-current"]["quality_checked_at_utc"],
            "2026-10-01T00:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()

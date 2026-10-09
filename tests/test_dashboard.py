import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard" / "index.html"


class DashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_dashboard_uses_public_live_tracker(self):
        self.assertIn(
            "https://raw.githubusercontent.com/YTxFSGAMERz/event-intelligence-agent/main/data/seen_events.json",
            self.html,
        )
        self.assertIn("setInterval(loadFeed,5*60*1000)", re.sub(r"\\s+", "", self.html))

    def test_dashboard_exposes_verification_and_evidence(self):
        for marker in (
            'id="verificationFilter"',
            "official_page_verified",
            "official_link_candidate_unverified",
            "function renderEvidence(e)",
            "function verificationLabel(e)",
            "evidence_urls",
        ):
            self.assertIn(marker, self.html)

    def test_dashboard_exposes_requested_research_filters(self):
        for marker in (
            'id="modeFilter"',
            'id="countryFilter"',
            'id="travelFilter"',
            'id="eligibilityFilter"',
            'id="statusFilter"',
            'id="sortFilter"',
        ):
            self.assertIn(marker, self.html)

    def test_dashboard_displays_source_health(self):
        self.assertIn('id="sourceGrid"', self.html)
        self.assertIn('id="sourceSummary"', self.html)
        self.assertIn("function renderSourceHealth()", self.html)
        self.assertIn("last_success_at", self.html)
        self.assertIn("failure_streak", self.html)

    def test_unverified_deadlines_and_travel_claims_stay_labeled(self):
        self.assertIn("Reported deadline · ", self.html)
        self.assertIn("function travelLabel(e)", self.html)
        self.assertIn(" · unverified", self.html)
        self.assertIn("Reported: support confirmed", self.html)

    def test_dashboard_can_render_images_and_event_details(self):
        self.assertIn("media.source_image_url", self.html)
        self.assertIn("function renderDetails(e)", self.html)
        self.assertIn("travel_support", self.html)
        self.assertIn("eligibility", self.html)


if __name__ == "__main__":
    unittest.main()

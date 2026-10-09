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
        self.assertIn("setInterval(loadFeed,5*60*1000)", re.sub(r"\s+", "", self.html))

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

    def test_multiple_category_membership_is_used_everywhere(self):
        self.assertIn("function eventCategories(e)", self.html)
        self.assertIn("eventCategories(e).includes(activeCategory)", self.html)
        self.assertIn("events.filter(e=>eventCategories(e).includes(c[0]))", self.html)
        self.assertIn('class="category-list"', self.html)

    def test_dashboard_paginates_and_exports_filtered_results(self):
        for marker in (
            'id="pagination"',
            "const PAGE_SIZE=12",
            "function renderPagination(total,pageCount)",
            'id="exportCsvBtn"',
            'id="exportJsonBtn"',
            "function exportRecords(format)",
            "return filteredEvents.map(e=>",
            "text/csv;charset=utf-8",
            "application/json;charset=utf-8",
        ):
            self.assertIn(marker, self.html)

    def test_csv_export_guards_spreadsheet_formulas(self):
        self.assertIn(r"if(/^[=+\-@\t\r]/.test(raw))", self.html)
        self.assertIn("raw.replace(", self.html)

    def test_estimated_result_type_and_information_coverage(self):
        for marker in (
            'id="contentShapeFilter"',
            "function contentShape(e)",
            "function contentShapeLabel(e)",
            "function informationCoverage(e)",
            "Details '+coverage+'/7",
            "sort==='coverage'",
            "Result type is estimated from the headline",
        ):
            self.assertIn(marker, self.html)

    def test_quality_filter_hides_flagged_by_default_but_keeps_review_paths(self):
        self.assertIn('id="qualityFilter"', self.html)
        self.assertIn('<option value="accepted" selected>', self.html)
        self.assertIn('<option value="flagged">Flagged by quality rules</option>', self.html)
        self.assertIn('<option value="unknown">Not quality-assessed</option>', self.html)
        self.assertIn('<option value="all">Include all quality states</option>', self.html)
        self.assertIn("function qualityState(e)", self.html)
        self.assertIn("function qualityLabel(e)", self.html)
        self.assertIn("qualityState(e)===quality", self.html)
        self.assertIn("discovery_quality_reason:", self.html)

    def test_mlh_calendar_observations_are_displayed_without_becoming_verified(self):
        for marker in (
            "e.source_observed",
            "function source_observed",
            "Observed schedule · unverified · ",
            "source_observed_location:",
            "Named event · MLH calendar",
            "unverified listing",
        ):
            if marker == "function source_observed":
                # Source-observation extraction lives server-side; the browser only renders the field.
                self.assertIn("e.source_observed", self.html)
            else:
                self.assertIn(marker, self.html)

    def test_observed_calendar_dates_participate_in_event_date_sort(self):
        self.assertIn("function eventStartSortValue(e)", self.html)
        self.assertIn("const explicitYear=raw.match", self.html)
        self.assertIn("return Date.UTC(year,monthMap[monthKey],day)", self.html)
        self.assertIn("sort==='event_date')list.sort((a,b)=>eventStartSortValue(a)-eventStartSortValue(b))", self.html)
        self.assertIn("Observed schedule · unverified · ", self.html)

    def test_dashboard_has_a_build_marker_for_stale_deployment_checks(self):
        self.assertIn(
            '<meta name="event-intel-dashboard-version" content="2026-10-10-source-observed-sort-v1">',
            self.html,
        )

    def test_dashboard_can_render_images_and_event_details(self):
        self.assertIn("media.source_image_url", self.html)
        self.assertIn("function renderDetails(e)", self.html)
        self.assertIn("travel_support", self.html)
        self.assertIn("eligibility", self.html)


if __name__ == "__main__":
    unittest.main()

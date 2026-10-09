import unittest
from datetime import datetime, timezone

import page_verification as verify


class FakeResponse:
    def __init__(self, url, body="", status_code=200, headers=None):
        self.url = url
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.routes.get(url)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise verify.requests.RequestException("no mock route")
        return response


EXPECTED = "International Student Hackathon Prize Challenge 2026"
DISCOVERY_URL = "https://publisher.example/news/42"
OFFICIAL_URL = "https://organizer.example/events/hackathon-2026"

OFFICIAL_HTML = """
<html><head>
<title>International Student Hackathon Prize Challenge 2026</title>
<meta name="applicationDeadline" content="October 31, 2026">
<script type="application/ld+json">
{
  "@context":"https://schema.org",
  "@type":"Event",
  "name":"International Student Hackathon Prize Challenge 2026",
  "startDate":"2026-11-14T09:00:00+05:30",
  "endDate":"2026-11-15T18:00:00+05:30",
  "eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode",
  "eventStatus":"https://schema.org/EventScheduled",
  "location":{"@type":"Place","name":"Innovation Centre","address":{
    "@type":"PostalAddress","addressLocality":"Pune",
    "addressRegion":"Maharashtra","addressCountry":"IN"}},
  "organizer":{"@type":"Organization","name":"Example Hackathon Foundation",
    "url":"https://organizer.example/"},
  "image":"https://organizer.example/media/event.jpg"
}
</script></head><body>
<h1>International Student Hackathon Prize Challenge 2026</h1>
<p>Application deadline: October 31, 2026.</p>
<p>Eligibility: Open to undergraduate computer science students.</p>
<p>Flights are covered. Accommodation is provided for selected participants.</p>
<p>Meals are not included.</p>
</body></html>
"""


class PageVerificationTests(unittest.TestCase):
    def test_rejects_local_hosts_private_ips_and_unsafe_schemes(self):
        for url in (
            "file:///etc/passwd",
            "http://localhost/page",
            "http://127.0.0.1/page",
            "http://192.168.1.8/page",
            "http://169.254.169.254/latest/meta-data/",
            "https://example.org:8443/page",
            "https://user:pass@example.org/page",
        ):
            with self.subTest(url=url):
                self.assertFalse(verify.is_safe_public_url(url))
        self.assertTrue(verify.is_safe_public_url("https://example.org/events/1"))

    def test_redirects_are_manually_followed_and_revalidated(self):
        routes = {
            "https://publisher.example/old": FakeResponse(
                "https://publisher.example/old", status_code=302,
                headers={"Location": "https://publisher.example/new"},
            ),
            "https://publisher.example/new": FakeResponse(
                "https://publisher.example/new", "<html><title>Event</title></html>"
            ),
        }
        budget = verify.VerificationBudget(2)
        result = verify.fetch_public_html(
            "https://publisher.example/old", budget=budget, session=FakeSession(routes)
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.final_url, "https://publisher.example/new")
        self.assertEqual(budget.pages_used, 1)

        bad_routes = {
            "https://publisher.example/old": FakeResponse(
                "https://publisher.example/old", status_code=302,
                headers={"Location": "http://127.0.0.1/admin"},
            )
        }
        denied = verify.fetch_public_html(
            "https://publisher.example/old", budget=verify.VerificationBudget(1),
            session=FakeSession(bad_routes),
        )
        self.assertEqual(denied.status, "redirect_rejected")

    def test_organizers_site_cta_is_recognized_as_explicit_official_link(self):
        html = """
        <html><head><title>International Student Hackathon Prize Challenge 2026</title></head>
        <body><h1>International Student Hackathon Prize Challenge 2026</h1>
        <a href="https://organizer.example/events/42">Enter on the organiser’s site</a>
        </body></html>
        """
        page = verify.PageFetch(
            "https://publisher.example/news/42",
            "https://publisher.example/news/42",
            "success",
            200,
            html=html,
        )
        facts = verify._extract_facts(page, EXPECTED)
        candidate, reason, explicit = verify._candidate_official_link(
            facts, page.final_url
        )
        self.assertEqual(candidate, "https://organizer.example/events/42")
        self.assertIn("organiser", reason.lower())
        self.assertTrue(explicit)

    def test_official_anchor_and_schema_org_event_extract_verified_facts(self):
        article = """
        <html><head><title>International Student Hackathon Prize Challenge 2026 News</title>
        <meta name="description" content="Details about the hackathon"></head><body>
        <h1>International Student Hackathon Prize Challenge 2026</h1>
        <a href="https://organizer.example/events/hackathon-2026">Official Website</a>
        </body></html>
        """
        routes = {
            DISCOVERY_URL: FakeResponse(DISCOVERY_URL, article),
            OFFICIAL_URL: FakeResponse(OFFICIAL_URL, OFFICIAL_HTML),
        }
        # The anchor target in the fixture uses the selected URL exactly.
        routes[OFFICIAL_URL] = routes[OFFICIAL_URL]
        routes[OFFICIAL_URL] = FakeResponse(OFFICIAL_URL, OFFICIAL_HTML)
        session = FakeSession(routes)
        budget = verify.VerificationBudget(4)
        result = verify.verify_opportunity_page(
            DISCOVERY_URL, EXPECTED, budget=budget, session=session
        )

        self.assertTrue(result["official_page_verified"])
        self.assertEqual(result["status"], "official_page_verified")
        self.assertEqual(result["official_url"], OFFICIAL_URL)
        self.assertEqual(result["verified_facts"]["organizer"], "Example Hackathon Foundation")
        self.assertEqual(result["verified_facts"]["event_start_at"], "2026-11-14T09:00:00+05:30")
        self.assertEqual(result["verified_facts"]["event_end_at"], "2026-11-15T18:00:00+05:30")
        self.assertEqual(result["verified_facts"]["location"]["mode"], "in_person")
        self.assertEqual(result["verified_facts"]["location"]["city"], "Pune")
        self.assertEqual(result["verified_facts"]["location"]["country_code"], "IN")
        self.assertEqual(result["verified_facts"]["deadline"]["normalized"], "2026-10-31")
        self.assertEqual(result["verified_facts"]["travel_support"]["flight"], "confirmed")
        self.assertEqual(result["verified_facts"]["travel_support"]["accommodation"], "confirmed")
        self.assertEqual(result["verified_facts"]["travel_support"]["meals"], "not_offered")
        self.assertIn("application_deadline_raw", result["fact_evidence"])
        self.assertLessEqual(budget.pages_used, 4)

    def test_publisher_article_without_official_link_stays_unverified(self):
        html = """
        <html><head><title>International Student Hackathon Prize Challenge 2026 News</title></head>
        <body><h1>International Student Hackathon Prize Challenge 2026</h1>
        <p>Application deadline: October 31, 2026.</p>
        <p>Fully funded travel.</p></body></html>
        """
        session = FakeSession({DISCOVERY_URL: FakeResponse(DISCOVERY_URL, html)})
        result = verify.verify_opportunity_page(
            DISCOVERY_URL, EXPECTED, budget=verify.VerificationBudget(2), session=session
        )
        self.assertFalse(result["official_page_verified"])
        self.assertIsNone(result["official_url"])
        self.assertEqual(result["status"], "source_page_checked")
        self.assertEqual(result["observed_facts"]["deadline"]["normalized"], "2026-10-31")
        self.assertEqual(result["verified_facts"], {})

    def test_missing_year_does_not_get_a_synthetic_deadline(self):
        normalized, precision = verify._explicit_date("October 31")
        self.assertIsNone(normalized)
        self.assertEqual(precision, "unknown")

    def test_verification_due_policy_retries_failures_sooner(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        recent_failure = {"verification": {
            "version": verify.VERIFICATION_VERSION,
            "status": "request_error",
            "last_checked_at": "2026-10-08T00:00:00+00:00",
        }}
        recent_page = {"verification": {
            "version": verify.VERIFICATION_VERSION,
            "status": "source_page_checked",
            "last_checked_at": "2026-10-08T00:00:00+00:00",
        }}
        week_old_page = {"verification": {
            "version": verify.VERIFICATION_VERSION,
            "status": "source_page_checked",
            "last_checked_at": "2026-10-01T00:00:00+00:00",
        }}
        self.assertTrue(verify.verification_is_due(recent_failure, now=now))
        self.assertFalse(verify.verification_is_due(recent_page, now=now))
        self.assertTrue(verify.verification_is_due(week_old_page, now=now))

    def test_page_budget_exhaustion_does_not_make_network_call(self):
        budget = verify.VerificationBudget(0)
        session = FakeSession({})
        result = verify.fetch_public_html(
            "https://publisher.example/page", budget=budget, session=session
        )
        self.assertEqual(result.status, "skipped_budget")
        self.assertEqual(session.calls, [])
        self.assertEqual(budget.pages_used, 0)


if __name__ == "__main__":
    unittest.main()

import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from datetime import datetime, timezone

import source_adapters
from source_adapters import (
    FeedResult,
    build_adapters,
    build_source_config,
    normalize_http_url,
    source_health_failure,
    source_health_success,
)


class FakeResponse:
    def __init__(self, url, content=b"", status_code=200):
        self.url = url
        self.content = content
        self.status_code = status_code
        self.encoding = "utf-8"
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class SourceAdapterTests(unittest.TestCase):
    def test_registry_selects_specialized_adapters_and_deduplicates(self):
        google = "https://news.google.com/rss/search?q=hackathon"
        hackalendar = "https://hackalendar.com/feed.xml"
        blog = "https://github.blog/changelog/feed/"
        mlh = "https://mlh.com/events"
        devfolio = "https://devfolio.co/explore"
        nsp = "https://scholarships.gov.in/All-Scholarships"
        unstop = "https://unstop.com/compete/amp"
        adapters = build_adapters([google, hackalendar, blog, mlh, devfolio, nsp, unstop, google])

        self.assertEqual(len(adapters), 7)
        self.assertIsInstance(adapters[0], source_adapters.GoogleNewsRSSAdapter)
        self.assertIsInstance(adapters[1], source_adapters.HackalendarRSSAdapter)
        self.assertIsInstance(adapters[2], source_adapters.OfficialBlogRSSAdapter)
        self.assertIsInstance(adapters[3], source_adapters.MLHEventsHTMLAdapter)
        self.assertIsInstance(adapters[4], source_adapters.DevfolioExploreHTMLAdapter)
        self.assertIsInstance(adapters[5], source_adapters.NSPScholarshipHTMLAdapter)
        self.assertIsInstance(adapters[6], source_adapters.UnstopExploreHTMLAdapter)
        self.assertEqual(adapters[0].config.adapter_type, "google_news_rss")
        self.assertEqual(adapters[1].config.adapter_type, "hackalendar_rss")
        self.assertEqual(adapters[2].config.adapter_type, "official_blog_rss")
        self.assertEqual(adapters[3].config.adapter_type, "mlh_events_html")
        self.assertEqual(adapters[3].config.access_method, "public_official_mlh_events_html")
        self.assertEqual(adapters[3].config.pagination_mode, "official_calendar_upcoming_section")
        self.assertEqual(adapters[4].config.adapter_type, "devfolio_html")
        self.assertEqual(adapters[4].config.pagination_mode, "platform_open_upcoming_sections")
        self.assertEqual(adapters[5].config.adapter_type, "nsp_scholarships_html")
        self.assertEqual(adapters[6].config.adapter_type, "unstop_html")
        self.assertEqual(adapters[5].config.pagination_mode, "portal_scheme_list_current_year")
        self.assertEqual(adapters[1].config.access_method, "public_hackalendar_rss")
        self.assertEqual(adapters[1].config.pagination_mode, "catalogue_feed_upcoming_events")
        self.assertNotEqual(adapters[0].config.source_id, adapters[1].config.source_id)
        self.assertIn("Devpost", build_source_config(
            "https://news.google.com/rss/search?q=site%3Adevpost.com%2Fhackathons"
        ).name)

    def test_unstop_parser_keeps_current_detail_links_and_skips_expired_or_external_links(self):
        html = b"""<!doctype html><html><body>
        <h2>Competitions</h2>
        <a href="/hackathons/codeverse-india-national-level-hackathon-wecodecoders-1763003">
          hackathons CodeVerse India - National Level Hackathon WeCodeCoders 1504 Applied 26 days left All Posted 10 Oct
        </a>
        <a href="https://unstop.com/competitions/snapdragon-ai-lab-challenge-1763004">
          Snapdragon AI Lab Challenge 2400 Registered 18 days left
        </a>
        <a href="/hackathons/past-event-1763005">Expired Past Event 10 days ago</a>
        <a href="https://outside.example/hackathons/unsafe-event-1763006">External Event 4 days left</a>
        <a href="/hackathons">Browse Hackathons</a>
        </body></html>"""
        adapter = build_adapters(["https://unstop.com/compete/amp"])[0]
        response = FakeResponse("https://unstop.com/compete/amp", html)
        result = adapter.fetch(session=FakeSession(response))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 2)
        self.assertEqual(result.entries[0]["title"], "CodeVerse India - National Level Hackathon WeCodeCoders")
        self.assertEqual(result.entries[0]["link"], "https://unstop.com/hackathons/codeverse-india-national-level-hackathon-wecodecoders-1763003")
        self.assertEqual(result.entries[0]["listing_category"], "hackathons")
        self.assertEqual(result.entries[0]["listing_countdown_text"], "26 days left")
        self.assertIn("discovery evidence only", result.entries[0]["summary"])
        self.assertEqual(result.entries[1]["listing_category"], "competitions")
        self.assertTrue(response.closed)

    def test_unstop_fails_closed_if_page_has_no_opportunity_detail_cards(self):
        adapter = build_adapters(["https://unstop.com/compete/amp"])[0]
        response = FakeResponse("https://unstop.com/compete/amp", b"<html><h1>Please enable cookies</h1></html>")
        with self.assertRaisesRegex(RuntimeError, "no opportunity detail cards"):
            adapter.fetch(session=FakeSession(response))
        self.assertTrue(response.closed)

    def test_mlh_calendar_parses_only_dated_upcoming_organizer_links(self):
        page_url = "https://www.mlh.com/seasons/2027/events/"
        html = b"""<!doctype html><html><body>
        <h2>Upcoming Events</h2>
        <h3>2026</h3>
        <a href="https://hacknc.com/"><span>Chapel Hill, North Carolina HackNC OCT 09 - 11 Chapel Hill, North Carolina, US In-Person</span></a>
        <a href="https://hackbios.xyz/"><span>Bhilai, Chhattisgarh HackBIOS OCT 09 - 10 Bhilai, Chhattisgarh, IN In-Person</span></a>
        <a href="https://hackcbs.tech/"><span>New delhi, New delhi hackCBS 9.O OCT 31 - NOV 01 New delhi, IN In-Person</span></a>
        <a href="/seasons/2027/events/"><span>Filter events OCT 10</span></a>
        <h2>Past Events</h2>
        <a href="https://past-event.example/"><span>Past Event OCT 03 - 04 Somewhere, US In-Person</span></a>
        </body></html>"""
        adapter = build_adapters(["https://mlh.com/events"])[0]
        response = FakeResponse(page_url, html)
        session = FakeSession(response)

        result = adapter.fetch(session=session)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 3)
        self.assertEqual([item["title"] for item in result.entries], ["HackNC", "HackBIOS", "hackCBS 9.O"])
        self.assertEqual(result.entries[0]["link"], "https://hacknc.com")
        self.assertIn("OCT 09 - 11", result.entries[0]["summary"])
        self.assertIn("Calendar section year: 2026", result.entries[0]["summary"])
        self.assertTrue(response.closed)
        self.assertEqual(len(session.calls), 1)

    def test_mlh_calendar_fails_closed_if_upcoming_section_is_missing(self):
        adapter = build_adapters(["https://mlh.com/events"])[0]
        response = FakeResponse("https://www.mlh.com/events", b"<html><h2>Blog</h2></html>")
        session = FakeSession(response)
        with self.assertRaisesRegex(RuntimeError, "Upcoming Events heading was not found"):
            adapter.fetch(session=session)
        self.assertTrue(response.closed)

    def test_registry_classifies_direct_devfolio_and_nsp_sources(self):
        devfolio = build_source_config("https://devfolio.co/explore")
        nsp = build_source_config("https://scholarships.gov.in/All-Scholarships")
        unstop = build_source_config("https://unstop.com/compete/amp")
        self.assertEqual(devfolio.adapter_type, "devfolio_html")
        self.assertEqual(devfolio.access_method, "public_devfolio_explore_html")
        self.assertEqual(nsp.adapter_type, "nsp_scholarships_html")
        self.assertEqual(nsp.access_method, "public_official_nsp_scholarship_html")
        self.assertEqual(unstop.adapter_type, "unstop_html")
        self.assertEqual(unstop.access_method, "public_unstop_html_listing")

    def test_devfolio_parser_keeps_open_and_upcoming_cards_but_ignores_past(self):
        html = b"""<!doctype html><html><body>
        <h2>Open</h2>
        <a href="https://wild-bugs.devfolio.co/"><strong>Wild Bugs</strong><span>Hackathon</span><span>Online</span><span>Open</span><span>Starts 14/10/26</span><span>Apply now</span></a>
        <a href="https://winter-of-code-4.devfolio.co/"><strong>Winter of Code 2026</strong><span>Hackathon</span><span>Online</span><span>Open</span><span>Starts 15/10/26</span></a>
        <h2>Upcoming</h2>
        <a href="https://lean-in-hacks-8.devfolio.co/"><strong>Lean In Hacks 8.0</strong><span>Online</span><span>Upcoming</span><span>Opens 20/10/26</span><span>Remind me</span></a>
        <h2>Past</h2>
        <a href="https://past-event.devfolio.co/"><strong>Past Event</strong><span>Ended</span></a>
        </body></html>"""
        adapter = build_adapters(["https://devfolio.co/explore"])[0]
        response = FakeResponse("https://devfolio.co/explore", html)
        result = adapter.fetch(session=FakeSession(response))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 3)
        self.assertEqual([item["title"] for item in result.entries], ["Wild Bugs", "Winter of Code 2026", "Lean In Hacks 8.0"])
        self.assertEqual(result.entries[0]["event_date_text"], "14/10/26")
        self.assertEqual(result.entries[0]["format_text"], "Online")
        self.assertEqual(result.entries[2]["application_open_date_text"], "20/10/26")
        self.assertIsNone(result.entries[2]["event_date_text"])
        self.assertIn("not independently verified as an organizer page", result.entries[0]["summary"])
        self.assertTrue(response.closed)

    def test_devfolio_fails_closed_if_no_current_event_cards_are_present(self):
        adapter = build_adapters(["https://devfolio.co/explore"])[0]
        response = FakeResponse("https://devfolio.co/explore", b"<html><h2>Blog</h2></html>")
        with self.assertRaisesRegex(RuntimeError, "no current event cards"):
            adapter.fetch(session=FakeSession(response))
        self.assertTrue(response.closed)

    def test_devfolio_can_parse_current_cards_without_heading_tags_and_ignores_ended_cards(self):
        html = b"""<!doctype html><html><body>
        <div><a href="https://wild-bugs.devfolio.co/"><span>Wild Bugs</span><span>Hackathon</span><span>Online</span><span>Open</span><span>Starts 14/10/26</span><span>Apply now</span></a></div>
        <div><a href="https://past-event.devfolio.co/"><span>Past Event</span><span>Ended</span><span>See projects</span></a></div>
        </body></html>"""
        adapter = build_adapters(["https://devfolio.co/explore"])[0]
        response = FakeResponse("https://devfolio.co/explore", html)
        result = adapter.fetch(session=FakeSession(response))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 1)
        self.assertEqual(result.entries[0]["title"], "Wild Bugs")
        self.assertEqual(result.entries[0]["listing_status"], "open")
        self.assertEqual(result.entries[0]["event_date_text"], "14/10/26")
        self.assertTrue(response.closed)

    def test_nsp_parser_extracts_scheme_and_application_deadline_without_guessing_links(self):
        html = b"""<!doctype html><html><body>
        <h5>Schemes On NSP</h5>
        <h6>AICTE - Swanath Scholarship Scheme (Technical Degree)</h6>
        <p>Scheme Open from : 01-06-2026 Student Application Open till : 31-10-2026</p>
        <a href="/public/schemeGuidelines/AICTE/AICTE_Swanath.pdf">Specifications</a>
        <a href="/public/faq/swanath.pdf">FAQ</a>
        <h6>National Scholarship for Post Graduate Studies</h6>
        <p>Scheme Open from : 16-09-2026 Student Application Open till : 30-11-2026</p>
        <a href="/public/schemeGuidelines/UGC/PG.pdf">Specifications</a>
        </body></html>"""
        adapter = build_adapters(["https://scholarships.gov.in/All-Scholarships"])[0]
        response = FakeResponse("https://scholarships.gov.in/All-Scholarships", html)
        result = adapter.fetch(session=FakeSession(response))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 2)
        first = result.entries[0]
        self.assertEqual(first["title"], "AICTE - Swanath Scholarship Scheme (Technical Degree)")
        self.assertEqual(first["deadline_raw"], "31-10-2026")
        self.assertEqual(first["deadline_display"], "October 31, 2026")
        self.assertEqual(first["link"], "https://scholarships.gov.in/public/schemeGuidelines/AICTE/AICTE_Swanath.pdf")
        self.assertIn("Official portal listing: https://scholarships.gov.in/All-Scholarships", first["summary"])
        self.assertTrue(response.closed)

    def test_nsp_fails_closed_if_page_has_no_scheme_guidance_links(self):
        html = b"""<!doctype html><html><body><h6>Example Scholarship Scheme Test</h6><p>Student Application Open till : 31-10-2026</p></body></html>"""
        adapter = build_adapters(["https://scholarships.gov.in/All-Scholarships"])[0]
        response = FakeResponse("https://scholarships.gov.in/All-Scholarships", html)
        with self.assertRaisesRegex(RuntimeError, "no schemes with specific official guidance links"):
            adapter.fetch(session=FakeSession(response))
        self.assertTrue(response.closed)

    def test_nsp_parser_never_treats_academic_year_heading_as_a_scheme(self):
        html = b"""<!doctype html><html><body>
        <h6>Academic Year 2026-27</h6>
        <a href="#">Specifications</a><a href="#">FAQ</a>
        </body></html>"""
        adapter = build_adapters(["https://scholarships.gov.in/All-Scholarships"])[0]
        response = FakeResponse("https://scholarships.gov.in/All-Scholarships", html)
        with self.assertRaisesRegex(RuntimeError, "no scholarship-scheme title candidates"):
            adapter.fetch(session=FakeSession(response))
        self.assertTrue(response.closed)

    def test_targeted_student_sources_receive_descriptive_labels(self):
        scholarship_query = build_source_config(
            "https://news.google.com/rss/search?q=site%3Ascholarships.gov.in+student"
        )
        student_program_query = build_source_config(
            "https://news.google.com/rss/search?q=site%3Ablog.google%2Fcompany-news%2Foutreach-and-initiatives%2Fstudent-programs+student"
        )
        self.assertEqual(
            scholarship_query.name,
            "Google News · National Scholarship Portal",
        )
        self.assertEqual(
            student_program_query.name,
            "Google News · Google Student Programs",
        )
        self.assertEqual(scholarship_query.adapter_type, "google_news_rss")
        self.assertEqual(student_program_query.adapter_type, "google_news_rss")

    def test_normalize_url_removes_tracking_parameters(self):
        result = normalize_http_url("HTTPS://Example.org/event/?utm_source=test&ref=feed&id=12#details")
        self.assertEqual(result, "https://example.org/event?id=12")

    def test_generic_source_keeps_a_direct_link(self):
        adapter = build_adapters(["https://example.org/feed.xml"])[0]
        resolved, status = adapter.resolve_item_url("https://publisher.example/a/?utm_source=feed")
        self.assertEqual(resolved, "https://publisher.example/a")
        self.assertEqual(status, "direct_link")

    def test_fetch_parses_rss_and_records_duration(self):
        xml = (
            b'<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>'
            b'<link>https://example.org</link><description>Feed</description>'
            b'<item><title>Student Hackathon</title><link>https://example.org/events/1</link>'
            b'<description>Register for the hackathon</description></item></channel></rss>'
        )
        adapter = build_adapters(["https://example.org/feed.xml"])[0]
        result = adapter.fetch(session=FakeSession(FakeResponse("https://example.org/feed.xml", xml)))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 1)
        self.assertEqual(result.entries[0].title, "Student Hackathon")
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_google_news_decoder_resolves_modern_encoded_urls(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=hackathon"])[0]
        original = source_adapters._decode_google_news
        try:
            source_adapters._decode_google_news = lambda url, timeout: {
                "success": True,
                "decoded_url": "https://publisher.example/articles/123?utm_source=test",
            }
            resolved, status = adapter.resolve_item_url(
                "https://news.google.com/rss/articles/encoded"
            )
        finally:
            source_adapters._decode_google_news = original
        self.assertEqual(resolved, "https://publisher.example/articles/123")
        self.assertEqual(status, "decoder_resolved")

    def test_google_news_decoder_failure_emits_diagnostic_and_keeps_unknown(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=hackathon"])[0]
        original = source_adapters._decode_google_news
        response = FakeResponse("https://news.google.com/rss/articles/encoded", b"<html><head></head>")
        try:
            source_adapters._decode_google_news = lambda url, timeout: {
                "success": False,
                "message": "signature   fields\nmissing",
            }
            output = StringIO()
            with patch("source_adapters.requests.get", return_value=response):
                with redirect_stdout(output):
                    resolved, status = adapter.resolve_item_url(
                        "https://news.google.com/rss/articles/encoded",
                        session=source_adapters.requests,
                    )
        finally:
            source_adapters._decode_google_news = original
        self.assertIsNone(resolved)
        self.assertEqual(status, "unresolved_google_news_link")
        self.assertIn("decoder did not resolve", output.getvalue().lower())
        self.assertIn("signature fields missing", output.getvalue().lower())

    def test_google_news_redirect_resolves_publisher_url(self):
        response = FakeResponse("https://publisher.example/events/abc/?utm_campaign=rss")
        session = FakeSession(response)
        adapter = build_adapters(["https://news.google.com/rss/search?q=hackathon"])[0]
        resolved, status = adapter.resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=session,
        )
        self.assertEqual(resolved, "https://publisher.example/events/abc")
        self.assertEqual(status, "redirect_resolved")
        self.assertTrue(response.closed)

    def test_google_news_canonical_tag_is_used_if_redirect_stays_on_google(self):
        html = (
            b'<html><head><link rel="canonical" '
            b'href="https://publisher.example/story?id=7&amp;utm_source=rss"></head><body>'
        )
        response = FakeResponse("https://news.google.com/rss/articles/encoded", html)
        session = FakeSession(response)
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        resolved, status = adapter.resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=session,
        )
        self.assertEqual(resolved, "https://publisher.example/story?id=7")
        self.assertEqual(status, "canonical_tag_resolved")

    def test_unresolved_google_url_remains_unknown(self):
        response = FakeResponse("https://news.google.com/rss/articles/encoded", b"<html><head></head>")
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        resolved, status = adapter.resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=FakeSession(response),
        )
        self.assertIsNone(resolved)
        self.assertEqual(status, "unresolved_google_news_link")

    def test_source_metadata_describes_access_fields_timeout_and_poll_interval(self):
        config = build_source_config(
            "https://news.google.com/rss/search?q=site%3Adevpost.com%2Fhackathons"
        )
        self.assertEqual(config.access_method, "public_google_news_rss")
        self.assertIn("title", config.expected_fields)
        self.assertIn("link", config.expected_fields)
        self.assertIn("media", config.expected_fields)
        self.assertEqual(config.timeout_seconds, 10)
        self.assertEqual(config.minimum_interval_seconds, 900)
        self.assertIn("feed_managed", config.pagination_mode)

    def test_minimum_poll_interval_is_enforced_using_last_real_attempt(self):
        config = build_source_config("https://example.org/feed.xml")
        now = datetime(2026, 10, 9, 10, 20, tzinfo=timezone.utc)
        recent = {"last_attempt_at": "2026-10-09T10:10:00+00:00"}
        old = {"last_attempt_at": "2026-10-09T10:00:00+00:00"}
        malformed = {"last_attempt_at": "not-a-date"}

        self.assertFalse(source_adapters.should_poll_source(recent, config, now=now))
        self.assertTrue(source_adapters.should_poll_source(old, config, now=now))
        self.assertTrue(source_adapters.should_poll_source(malformed, config, now=now))
        self.assertTrue(source_adapters.should_poll_source({}, config, now=now))

    def test_skipped_poll_preserves_last_attempt_and_last_success(self):
        config = build_source_config("https://example.org/feed.xml")
        previous = {
            "last_attempt_at": "2026-10-09T10:00:00+00:00",
            "last_success_at": "2026-10-09T10:00:00+00:00",
            "last_success_items_seen": 19,
        }
        skipped = source_adapters.source_health_skipped(
            previous, config, skipped_at="2026-10-09T10:05:00+00:00"
        )
        self.assertEqual(skipped["last_status"], "skipped_minimum_interval")
        self.assertEqual(skipped["last_attempt_at"], previous["last_attempt_at"])
        self.assertEqual(skipped["last_success_at"], previous["last_success_at"])
        self.assertEqual(skipped["last_success_items_seen"], 19)
        self.assertEqual(skipped["last_skipped_at"], "2026-10-09T10:05:00+00:00")
        self.assertEqual(skipped["timeout_seconds"], 10)
        self.assertEqual(skipped["minimum_interval_seconds"], 900)

    def test_url_resolution_budget_bounds_requests_and_reserves_new_items(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        budget = source_adapters.URLResolutionBudget(max_total=2, max_legacy=1)
        session = FakeSession(FakeResponse("https://publisher.example/legacy"))

        resolved, status = budget.resolve(
            adapter, "https://news.google.com/rss/articles/legacy",
            session=session, legacy=True,
        )
        self.assertEqual(resolved, "https://publisher.example/legacy")
        self.assertEqual(status, "redirect_resolved")
        self.assertEqual(budget.used_total, 1)
        self.assertEqual(budget.used_legacy, 1)

        deferred_legacy, legacy_status = budget.resolve(
            adapter, "https://news.google.com/rss/articles/older",
            session=session, legacy=True,
        )
        self.assertIsNone(deferred_legacy)
        self.assertEqual(legacy_status, "legacy_resolution_budget_deferred")
        self.assertEqual(budget.used_total, 1)

        session.response = FakeResponse("https://publisher.example/new")
        resolved_new, new_status = budget.resolve(
            adapter, "https://news.google.com/rss/articles/new",
            session=session, legacy=False,
        )
        self.assertEqual(resolved_new, "https://publisher.example/new")
        self.assertEqual(new_status, "redirect_resolved")
        self.assertEqual(budget.used_total, 2)

        deferred_new, budget_status = budget.resolve(
            adapter, "https://news.google.com/rss/articles/another",
            session=session,
        )
        self.assertIsNone(deferred_new)
        self.assertEqual(budget_status, "resolution_budget_deferred")
        self.assertEqual(budget.used_total, 2)

    def test_direct_rss_links_do_not_consume_google_resolution_budget(self):
        adapter = build_adapters(["https://example.org/feed.xml"])[0]
        budget = source_adapters.URLResolutionBudget(max_total=1, max_legacy=1)
        resolved, status = budget.resolve(
            adapter, "https://publisher.example/event/?utm_source=rss"
        )
        self.assertEqual(resolved, "https://publisher.example/event")
        self.assertEqual(status, "direct_link")
        self.assertEqual(budget.used_total, 0)

    def test_source_health_failure_keeps_last_success(self):
        config = build_source_config("https://example.org/feed.xml")
        previous = {
            "last_success_at": "2026-10-08T10:00:00+00:00",
            "last_success_items_seen": 42,
            "failure_streak": 2,
        }
        failed = source_health_failure(previous, config, ValueError("bad feed"), 120)
        self.assertEqual(failed["last_success_at"], "2026-10-08T10:00:00+00:00")
        self.assertEqual(failed["last_success_items_seen"], 42)
        self.assertEqual(failed["failure_streak"], 3)
        self.assertEqual(failed["last_status"], "failure")

    def test_source_health_success_resets_failure_streak(self):
        config = build_source_config("https://example.org/feed.xml")
        previous = {"failure_streak": 3}
        result = FeedResult([], "success", 0, 25, "2026-10-09T10:00:00+00:00")
        successful = source_health_success(previous, config, result, 0, 0)
        self.assertEqual(successful["failure_streak"], 0)
        self.assertEqual(successful["last_success_at"], result.fetched_at_utc)
        self.assertEqual(successful["last_status"], "success")
        self.assertEqual(successful["last_success_items_seen"], 0)
        self.assertEqual(successful["access_method"], config.access_method)
        self.assertEqual(successful["expected_fields"], list(config.expected_fields))
        self.assertEqual(successful["timeout_seconds"], config.timeout_seconds)
        self.assertEqual(successful["minimum_interval_seconds"], config.minimum_interval_seconds)


if __name__ == "__main__":
    unittest.main()

import json
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
        unstop_hackathons = "https://unstop.com/hackathons/amp"
        unstop_internships = "https://unstop.com/internship-portal/amp"
        unstop_competitions = "https://unstop.com/competitions/amp"
        unstop_scholarships = "https://unstop.com/scholarships/amp"
        devfolio_catalogue = "https://devfolio.co/hackathons"
        devpost_api = "https://devpost.com/api/hackathons"
        adapters = build_adapters([google, hackalendar, blog, mlh, devfolio, nsp, unstop, unstop_hackathons, unstop_internships, unstop_competitions, unstop_scholarships, devfolio_catalogue, devpost_api, google])

        self.assertEqual(len(adapters), 13)
        self.assertIsInstance(adapters[0], source_adapters.GoogleNewsRSSAdapter)
        self.assertIsInstance(adapters[1], source_adapters.HackalendarRSSAdapter)
        self.assertIsInstance(adapters[2], source_adapters.OfficialBlogRSSAdapter)
        self.assertIsInstance(adapters[3], source_adapters.MLHEventsHTMLAdapter)
        self.assertIsInstance(adapters[4], source_adapters.DevfolioExploreHTMLAdapter)
        self.assertIsInstance(adapters[5], source_adapters.NSPScholarshipHTMLAdapter)
        self.assertIsInstance(adapters[6], source_adapters.UnstopExploreHTMLAdapter)
        self.assertIsInstance(adapters[7], source_adapters.UnstopExploreHTMLAdapter)
        self.assertIsInstance(adapters[8], source_adapters.UnstopExploreHTMLAdapter)
        self.assertIsInstance(adapters[9], source_adapters.UnstopExploreHTMLAdapter)
        self.assertIsInstance(adapters[10], source_adapters.UnstopExploreHTMLAdapter)
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
        self.assertEqual(adapters[6].config.name, "Unstop · Open Opportunities")
        self.assertEqual(adapters[7].config.name, "Unstop · Hackathons")
        self.assertEqual(adapters[8].config.name, "Unstop · Internships")
        self.assertEqual(adapters[9].config.name, "Unstop · Competitions")
        self.assertEqual(adapters[10].config.name, "Unstop · Scholarships")
        self.assertIsInstance(adapters[11], source_adapters.DevfolioExploreHTMLAdapter)
        self.assertEqual(adapters[11].config.name, "Devfolio · Hackathons Catalogue")
        self.assertIsInstance(adapters[12], source_adapters.DevpostHackathonsAPIAdapter)
        self.assertEqual(adapters[12].config.adapter_type, "devpost_api")
        self.assertEqual(adapters[12].config.name, "Devpost · Open Hackathons (JSON API)")
        self.assertEqual(adapters[5].config.pagination_mode, "portal_scheme_list_current_year")
        self.assertEqual(adapters[1].config.access_method, "public_hackalendar_rss")
        self.assertEqual(adapters[1].config.pagination_mode, "catalogue_feed_upcoming_events")
        self.assertNotEqual(adapters[0].config.source_id, adapters[1].config.source_id)
        self.assertIn("Devpost", build_source_config(
            "https://news.google.com/rss/search?q=site%3Adevpost.com%2Fhackathons"
        ).name)

    def test_devpost_api_parses_only_public_open_and_upcoming_entries(self):
        payload = {
            "hackathons": [
                {
                    "id": 30790,
                    "title": "Nebius x NVIDIA Global AI Hackathon",
                    "displayed_location": {"icon": "globe", "location": "Online"},
                    "open_state": "open",
                    "url": "https://nebiusglobalaihackathon.devpost.com/",
                    "time_left_to_submission": "20 days left",
                    "submission_period_dates": "Aug 26 - Oct 30, 2026",
                    "themes": [{"name": "Machine Learning/AI"}, {"name": "Beginner Friendly"}],
                    "prize_amount": "$<span data-currency-value>50,000</span>",
                    "registrations_count": 20810,
                    "organization_name": "nebius",
                    "invite_only": False,
                    "thumbnail_url": "//cdn.example.test/thumb.png",
                },
                {
                    "id": 30447,
                    "title": "Build With AI: Basics",
                    "displayed_location": {"location": "Online"},
                    "open_state": "upcoming",
                    "url": "https://learn-ai-basics.devpost.com/",
                    "time_left_to_submission": "16 days left",
                    "submission_period_dates": "Sep 22 - Oct 26, 2026",
                    "themes": [{"name": "Education"}],
                    "prize_amount": "$2,500",
                    "registrations_count": 3008,
                    "organization_name": "Devpost",
                    "invite_only": False,
                },
                {"id": 3, "title": "Ended Hackathon", "open_state": "ended", "url": "https://ended.devpost.com/"},
                {"id": 4, "title": "Private Hackathon", "open_state": "open", "url": "https://private.devpost.com/", "invite_only": True},
                {"id": 5, "title": "External Link Event", "open_state": "open", "url": "https://outside.example.com/event"},
            ],
            "meta": {"total_count": 5, "per_page": 9},
        }
        adapter = build_adapters(["https://devpost.com/api/hackathons"])[0]
        response = FakeResponse("https://devpost.com/api/hackathons", json.dumps(payload).encode("utf-8"))
        session = FakeSession(response)
        result = adapter.fetch(session=session)

        self.assertEqual(session.calls[0][1].get("params"), {"per_page": 50, "page": 1})
        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 2)
        self.assertEqual(
            [item["title"] for item in result.entries],
            ["Nebius x NVIDIA Global AI Hackathon", "Build With AI: Basics"],
        )
        self.assertEqual(result.entries[0]["link"], "https://nebiusglobalaihackathon.devpost.com")
        self.assertEqual(result.entries[0]["listing_status"], "Open")
        self.assertEqual(result.entries[0]["time_left_to_submission"], "20 days left")
        self.assertEqual(result.entries[0]["submission_period_dates"], "Aug 26 - Oct 30, 2026")
        self.assertIn("50,000", result.entries[0]["prize_text"])
        self.assertEqual(result.entries[0]["theme_names"], ["Machine Learning/AI", "Beginner Friendly"])
        self.assertEqual(result.entries[0]["thumbnail_url"], "https://cdn.example.test/thumb.png")
        self.assertEqual(result.entries[0]["media_thumbnail"][0]["url"], "https://cdn.example.test/thumb.png")
        self.assertIn("not official organizer verification", result.entries[0]["summary"])
        self.assertTrue(response.closed)

    def test_devpost_api_parses_fifty_entries_without_truncating(self):
        payload = {
            "hackathons": [
                {
                    "title": f"Student AI Hackathon {index:02d}",
                    "open_state": "open",
                    "url": f"https://student-ai-{index:02d}.devpost.com/",
                    "submission_period_dates": "Oct 1 - Oct 30, 2026",
                    "time_left_to_submission": "10 days left",
                    "invite_only": False,
                }
                for index in range(50)
            ],
            "meta": {"total_count": 14033, "per_page": 50},
        }
        adapter = build_adapters(["https://devpost.com/api/hackathons"])[0]
        response = FakeResponse("https://devpost.com/api/hackathons", json.dumps(payload).encode("utf-8"))
        result = adapter.fetch(session=FakeSession(response))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 50)
        self.assertEqual(result.entries[0]["title"], "Student AI Hackathon 00")
        self.assertEqual(result.entries[-1]["title"], "Student AI Hackathon 49")
        self.assertTrue(response.closed)

    def test_devpost_api_follows_page_numbers_when_endpoint_caps_page_size(self):
        def event_row(index):
            return {
                "title": f"Student AI Hackathon {index:02d}",
                "open_state": "open",
                "url": f"https://student-ai-{index:02d}.devpost.com/",
                "submission_period_dates": "Oct 1 - Oct 30, 2026",
                "time_left_to_submission": "10 days left",
                "invite_only": False,
            }

        payloads = [
            {"hackathons": [event_row(i) for i in range(9)], "meta": {"total_count": 18, "per_page": 9}},
            {"hackathons": [event_row(i) for i in range(9, 18)], "meta": {"total_count": 18, "per_page": 9}},
        ]

        class PagedSession:
            def __init__(self):
                self.calls = []
                self.all_responses = [
                    FakeResponse("https://devpost.com/api/hackathons", json.dumps(payload).encode("utf-8"))
                    for payload in payloads
                ]
                self.responses = list(self.all_responses)

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if not self.responses:
                    raise AssertionError("Adapter requested more pages than expected")
                return self.responses.pop(0)

        adapter = build_adapters(["https://devpost.com/api/hackathons"])[0]
        session = PagedSession()
        result = adapter.fetch(session=session)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 18)
        self.assertEqual(result.entries[0]["title"], "Student AI Hackathon 00")
        self.assertEqual(result.entries[-1]["title"], "Student AI Hackathon 17")
        self.assertEqual([call[1]["params"]["page"] for call in session.calls], [1, 2])
        self.assertTrue(all(call[1]["params"]["per_page"] == 50 for call in session.calls))
        self.assertTrue(all(response.closed for response in session.all_responses))

    def test_devpost_api_fails_closed_if_payload_shape_changes(self):
        adapter = build_adapters(["https://devpost.com/api/hackathons"])[0]
        response = FakeResponse("https://devpost.com/api/hackathons", b'{"events": []}')
        with self.assertRaisesRegex(RuntimeError, "hackathons list"):
            adapter.fetch(session=FakeSession(response))
        self.assertTrue(response.closed)

    def test_unstop_parser_keeps_current_detail_links_and_skips_expired_or_external_links(self):
        html = b"""<!doctype html><html><body>
        <h2>Competitions</h2>
        <a href="/hackathons/codeverse-india-national-level-hackathon-wecodecoders-1763003">
          hackathons Online Free CodeVerse India - National Level Hackathon WeCodeCoders 1504 Applied 26 days left All Posted 10 Oct
        </a>
        <a href="https://unstop.com/competitions/snapdragon-ai-lab-challenge-1763004">
          Snapdragon AI Lab Challenge 2400 Registered 18 days left
        </a>
        <a href="/hackathons/bugwars-iiitn-1766954">
          hackathons Launching soon BugWars IIIT Nagpur 1000 Registered 2 hours left
        </a>
        <a href="/competitions/asian-paints-alchemy-2026-asian-paints-1704498">
          Asian Paints Online Asian Paints Alchemy 2026 29330 Registered 26 days left
        </a>
        <a href="/scholarships/need-based-technical-scholarship-2026-1763006">
          Need-Based Technical Scholarship 2026 1,205 Applied 12 days left
        </a>
        <a href="/hackathons/past-event-1763005">Expired Past Event 10 days ago</a>
        <a href="https://outside.example/hackathons/unsafe-event-1763006">External Event 4 days left</a>
        <a href="/hackathons">Browse Hackathons</a>
        </body></html>"""
        adapter = build_adapters(["https://unstop.com/compete/amp"])[0]
        response = FakeResponse("https://unstop.com/compete/amp", html)
        result = adapter.fetch(session=FakeSession(response))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.item_count, 5)
        self.assertEqual(result.entries[0]["title"], "CodeVerse India - National Level Hackathon WeCodeCoders")
        self.assertEqual(result.entries[0]["link"], "https://unstop.com/hackathons/codeverse-india-national-level-hackathon-wecodecoders-1763003")
        self.assertEqual(result.entries[0]["listing_category"], "hackathons")
        self.assertEqual(result.entries[0]["listing_countdown_text"], "26 days left")
        self.assertIn("discovery evidence only", result.entries[0]["summary"])
        self.assertIn("Online Free CodeVerse India", result.entries[0]["summary"])
        self.assertEqual(result.entries[1]["listing_category"], "competitions")
        self.assertEqual(result.entries[2]["title"], "BugWars IIIT Nagpur")
        self.assertIn("Launching soon BugWars", result.entries[2]["summary"])
        self.assertEqual(result.entries[3]["title"], "Asian Paints Alchemy 2026")
        self.assertIn("Asian Paints Online Asian Paints Alchemy", result.entries[3]["summary"])
        self.assertEqual(result.entries[4]["title"], "Need-Based Technical Scholarship 2026")
        self.assertEqual(result.entries[4]["listing_category"], "scholarships")
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

    def test_registry_classifies_direct_devfolio_nsp_and_devpost_sources(self):
        devfolio = build_source_config("https://devfolio.co/explore")
        nsp = build_source_config("https://scholarships.gov.in/All-Scholarships")
        unstop = build_source_config("https://unstop.com/compete/amp")
        devpost = build_source_config("https://devpost.com/api/hackathons")
        self.assertEqual(devfolio.adapter_type, "devfolio_html")
        self.assertEqual(devfolio.access_method, "public_devfolio_explore_html")
        self.assertEqual(nsp.adapter_type, "nsp_scholarships_html")
        self.assertEqual(nsp.access_method, "public_official_nsp_scholarship_html")
        self.assertEqual(unstop.adapter_type, "unstop_html")
        self.assertEqual(unstop.access_method, "public_unstop_html_listing")
        self.assertEqual(devpost.adapter_type, "devpost_api")
        self.assertEqual(devpost.access_method, "public_devpost_json_api")
        self.assertEqual(devpost.pagination_mode, "public_api_page_size_50_max_3_pages")
        self.assertIn("theme_names", devpost.expected_fields)

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
        response = FakeResponse(
            "https://devfolio.co/explore",
            b"<html><h2>Blog</h2><p>Please enable cookies</p></html>",
        )
        with self.assertRaisesRegex(RuntimeError, "no current event cards") as raised:
            adapter.fetch(session=FakeSession(response))
        message = str(raised.exception)
        self.assertIn("html_bytes=", message)
        self.assertIn("tag_counts=", message)
        self.assertIn("has_cookie_notice=True", message)
        self.assertIn("has_access_challenge=False", message)
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

    def test_required_google_news_decoder_dependency_is_imported(self):
        self.assertIsNotNone(
            source_adapters._decode_google_news,
            "googlenewsdecoder is pinned in requirements.txt and must load in CI",
        )
        self.assertIsNone(source_adapters._GOOGLE_NEWS_DECODER_IMPORT_ERROR)

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
        self.assertEqual(config.minimum_interval_seconds, 600)
        self.assertIn("feed_managed", config.pagination_mode)

    def test_minimum_poll_interval_is_enforced_using_last_real_attempt(self):
        config = build_source_config("https://example.org/feed.xml")
        now = datetime(2026, 10, 9, 10, 20, tzinfo=timezone.utc)
        recent = {"last_attempt_at": "2026-10-09T10:11:00+00:00"}
        old = {"last_attempt_at": "2026-10-09T10:10:00+00:00"}
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
        self.assertEqual(skipped["minimum_interval_seconds"], 600)

    def test_google_news_decoder_batch_preserves_input_order(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        urls = [
            "https://news.google.com/rss/articles/article-one?oc=5",
            "https://news.google.com/rss/articles/article-two?oc=5",
        ]
        decoded = [
            {"success": True, "decoded_url": "https://publisher.example/one"},
            {"success": True, "decoded_url": "https://publisher.example/two"},
        ]

        output = StringIO()
        with patch("source_adapters._decode_google_news", return_value=decoded) as decoder:
            with redirect_stdout(output):
                results = adapter.resolve_item_urls(urls, timeout=7)

        self.assertEqual(
            results,
            [
                ("https://publisher.example/one", "decoder_resolved"),
                ("https://publisher.example/two", "decoder_resolved"),
            ],
        )
        decoder.assert_called_once_with(
            [
                "https://news.google.com/rss/articles/article-one?oc=5",
                "https://news.google.com/rss/articles/article-two?oc=5",
            ],
            timeout=7,
            interval=1,
        )
        self.assertIn("Google News batch resolution: links=2; mode=batch", output.getvalue())
        self.assertIn("decoder_resolved=2; http_fallbacks=0; final_resolved=2; unresolved=0", output.getvalue())

    def test_google_news_batch_reports_if_decoder_dependency_is_unavailable(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        urls = [
            "https://news.google.com/rss/articles/article-one",
            "https://news.google.com/rss/articles/article-two",
        ]
        response = FakeResponse(urls[0], b"<html><head></head><body></body></html>")
        output = StringIO()
        with patch("source_adapters._decode_google_news", None), patch(
            "source_adapters._GOOGLE_NEWS_DECODER_IMPORT_ERROR", "ImportError"
        ), patch("source_adapters.requests.get", return_value=response):
            with redirect_stdout(output):
                results = adapter.resolve_item_urls(urls)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(status == "unresolved_google_news_link" for _, status in results))
        self.assertIn("decoder unavailable (import_error=ImportError)", output.getvalue())
        self.assertIn("mode=unavailable", output.getvalue())
        self.assertIn("http_fallbacks=2", output.getvalue())

    def test_google_news_decoder_batch_falls_back_for_only_failed_items(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        urls = [
            "https://news.google.com/rss/articles/article-one?oc=5",
            "https://news.google.com/rss/articles/article-two?oc=5",
        ]
        decoded = [
            {"success": True, "decoded_url": "https://publisher.example/one"},
            {"success": False, "message": "Google rate limit"},
        ]
        response = FakeResponse(
            urls[1],
            b'<html><head><link rel="canonical" href="https://publisher.example/two"></head><body>x</body></html>',
        )

        with patch("source_adapters._decode_google_news", return_value=decoded), patch(
            "source_adapters.requests.get", return_value=response
        ) as get:
            results = adapter.resolve_item_urls(urls)

        self.assertEqual(results[0], ("https://publisher.example/one", "decoder_resolved"))
        self.assertEqual(results[1], ("https://publisher.example/two", "canonical_tag_resolved"))
        get.assert_called_once()
        self.assertTrue(response.closed)

    def test_url_resolution_budget_batches_legacy_and_preserves_reserved_capacity(self):
        adapter = build_adapters(["https://news.google.com/rss/search?q=test"])[0]
        budget = source_adapters.URLResolutionBudget(max_total=3, max_legacy=2)
        session = FakeSession(FakeResponse("https://publisher.example/resolved"))

        results = budget.resolve_many(
            adapter,
            [
                "https://news.google.com/rss/articles/legacy-one",
                "https://news.google.com/rss/articles/legacy-two",
                "https://news.google.com/rss/articles/legacy-three",
            ],
            session=session,
            legacy=True,
        )

        self.assertEqual(results[0], ("https://publisher.example/resolved", "redirect_resolved"))
        self.assertEqual(results[1], ("https://publisher.example/resolved", "redirect_resolved"))
        self.assertEqual(results[2], (None, "legacy_resolution_budget_deferred"))
        self.assertEqual(budget.used_total, 2)
        self.assertEqual(budget.used_legacy, 2)
        self.assertEqual(len(session.calls), 2)

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

"""Source adapter registry for Event Intel.

The registry deliberately keeps discovery separate from official verification:
a resolved publisher article URL is not proof that the opportunity's details
or deadline have been confirmed by the organizer.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import feedparser
import requests

try:
    from googlenewsdecoder import gnewsdecoder as _decode_google_news
except ImportError:  # Keep ordinary RSS sources functional if the optional decoder is unavailable.
    _decode_google_news = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host(url: str) -> str:
    return (urlsplit(str(url or "")).hostname or "").lower()


def normalize_http_url(url: str | None) -> str | None:
    """Normalize a public HTTP(S) URL without discarding meaningful query data."""
    candidate = str(url or "").strip()
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    query = []
    for component in parts.query.split("&"):
        if not component:
            continue
        key = component.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in {"ref", "source", "fbclid", "gclid"}:
            continue
        query.append(component)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "&".join(query), ""))


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    name: str
    url: str
    adapter_type: str
    access_method: str = "public_rss_atom"
    expected_fields: tuple[str, ...] = ("title", "link", "summary", "published")
    pagination_mode: str = "feed_managed_no_client_pagination"
    timeout_seconds: int = 10
    minimum_interval_seconds: int = 600
    rate_limit_policy: str = "minimum_poll_interval_enforced_by_agent"


@dataclass
class FeedResult:
    entries: list[Any]
    status: str
    item_count: int
    duration_ms: int
    fetched_at_utc: str


class CanonicalLinkParser(HTMLParser):
    """Extract canonical and social URL hints from a short HTML prefix."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag.lower() == "link" and "canonical" in attrs_dict.get("rel", "").lower().split():
            if attrs_dict.get("href"):
                self.urls.append(attrs_dict["href"])
        elif tag.lower() == "meta":
            key = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            if key in {"og:url", "twitter:url"} and attrs_dict.get("content"):
                self.urls.append(attrs_dict["content"])



MLH_MONTH = r"(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|JUL(?:Y)?|AUG(?:UST)?|SEP(?:T(?:EMBER)?)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
MLH_EVENT_DATE_RE = re.compile(
    r"\b" + MLH_MONTH + r"\s+\d{1,2}"
    r"(?:\s*-\s*(?:" + MLH_MONTH + r"\s+)?\d{1,2})?"
    r"(?:,\s*\d{4})?\b",
    re.IGNORECASE,
)
MLH_REGION_NAMES = (
    "British Columbia", "North Carolina", "South Carolina", "West Virginia",
    "New Brunswick", "Nova Scotia", "Prince Edward Island", "Newfoundland and Labrador",
    "County Durham", "Chhattisgarh", "Timiș", "Coahuila", "Ontario", "Quebec",
    "Alberta", "Manitoba", "Saskatchewan", "New York", "New Delhi", "Rhode Island",
    "Massachusetts", "Pennsylvania", "Mississippi", "North Dakota", "South Dakota",
    "New Hampshire", "New Jersey", "New Mexico", "Connecticut", "California",
    "Colorado", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
    "Michigan", "Minnesota", "Missouri", "Montana", "Nebraska", "Nevada", "Ohio",
    "Oklahoma", "Oregon", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "Wisconsin", "Wyoming", "Arizona", "Arkansas", "Alabama", "Alaska",
    "Auckland", "London", "England", "Spain", "BC", "ON", "AB", "NS", "QC", "CA",
    "SC", "TX", "WI", "PA", "MD", "NC", "MA", "GA", "TN", "CT", "RI", "VA",
    "IL", "FL", "NJ", "CO", "AZ", "UT", "WA", "NY", "IN", "GB",
)
_MLH_REGION_RE = re.compile(
    r",\s*(?:" + "|".join(
        re.escape(name) for name in sorted(set(MLH_REGION_NAMES), key=len, reverse=True)
    ) + r")\s+(?P<title>.+)$",
    re.IGNORECASE,
)


class MLHUpcomingEventsParser(HTMLParser):
    """Extract dated external links from the official MLH Upcoming Events section."""

    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_upcoming = False
        self.saw_upcoming_heading = False
        self.current_year: int | None = None
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._anchor: dict | None = None
        self.events: list[dict] = []
        self._seen_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "").strip() for key, value in attrs}
        tag = tag.lower()
        if tag in self.HEADING_TAGS:
            self._heading_tag = tag
            self._heading_parts = []
        if tag == "a":
            self._anchor = {
                "href": attrs_dict.get("href", ""),
                "aria_label": attrs_dict.get("aria-label", ""),
                "title_attr": attrs_dict.get("title", ""),
                "parts": [],
            }

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_parts.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._heading_tag == tag:
            heading = re.sub(r"\s+", " ", " ".join(self._heading_parts)).strip().casefold()
            if heading == "upcoming events":
                self.in_upcoming = True
                self.saw_upcoming_heading = True
                self.current_year = None
            elif heading == "past events":
                self.in_upcoming = False
                self.current_year = None
            elif self.in_upcoming and re.fullmatch(r"20\d{2}", heading):
                self.current_year = int(heading)
            self._heading_tag = None
            self._heading_parts = []

        if tag != "a" or self._anchor is None:
            return
        anchor = self._anchor
        self._anchor = None
        if not self.in_upcoming:
            return

        visible = re.sub(r"\s+", " ", " ".join(anchor["parts"])).strip()
        date_match = MLH_EVENT_DATE_RE.search(visible)
        if not date_match:
            return

        link = normalize_http_url(urljoin("https://www.mlh.com/", anchor.get("href", "")))
        if not link:
            return
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if host == "mlh.com" or host.endswith(".mlh.com") or parts.username or parts.password:
            return
        if link in self._seen_links:
            return

        prefix = visible[:date_match.start()].strip(" \t-–—|·:")
        region_match = _MLH_REGION_RE.search(prefix)
        title = (region_match.group("title") if region_match else prefix).strip(" ,:–—-|")
        if not title:
            title = anchor.get("aria_label") or anchor.get("title_attr") or visible
        title = re.sub(r"\s+", " ", title).strip()[:180]
        if not title:
            return

        suffix = visible[date_match.end():].strip(" \t-–—|·:")
        date_text = date_match.group(0).strip()
        year_text = str(self.current_year) if self.current_year else "year not provided by calendar"
        summary = (
            "Official MLH upcoming hackathon/event calendar listing. "
            f"Calendar section year: {year_text}. Displayed schedule: {date_text}. "
            f"Listing text: {visible}. Location/format text: {suffix or 'not separately labelled'}. "
            "The linked organizer page remains the source of eligibility, exact schedule, prizes and application rules."
        )
        self.events.append({
            "title": title,
            "link": link,
            "summary": summary,
            "event_date_text": date_text,
            "calendar_year": self.current_year,
            "location_text": suffix,
            "source_listing": "MLH Upcoming Events Calendar",
        })
        self._seen_links.add(link)



DEVFOLIO_DATE_RE = re.compile(r"\b(?P<label>starts|opens)\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\b", re.IGNORECASE)


class DevfolioExploreParser(HTMLParser):
    """Parse only Devfolio's Open and Upcoming hackathon sections."""

    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    META_TEXT = re.compile(
        r"^(?:hackathon|theme|no restrictions|online|offline|hybrid|open|upcoming|past|"
        r"apply now|remind me|live|starts?\s+\d{1,2}/\d{1,2}/\d{2,4}|"
        r"opens?\s+\d{1,2}/\d{1,2}/\d{2,4}|\+\s*[\d,]+\s+participating)$",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section = ""
        self.saw_open = False
        self.saw_upcoming = False
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._anchor: dict | None = None
        self._current: dict | None = None
        self.events: list[dict] = []
        self._seen_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "").strip() for key, value in attrs}
        tag = tag.lower()
        if tag in self.HEADING_TAGS:
            self._heading_tag = tag
            self._heading_parts = []
        if tag == "a":
            self._anchor = {
                "href": attrs_dict.get("href", ""),
                "aria_label": attrs_dict.get("aria-label", ""),
                "title_attr": attrs_dict.get("title", ""),
                "parts": [],
            }

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_parts.append(data)
        elif self._anchor is None and self._current is not None:
            self._current["parts"].append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def _finish_current(self) -> None:
        current = self._current
        self._current = None
        if not current:
            return
        parts = [re.sub(r"\s+", " ", str(value or "")).strip() for value in current.get("parts", [])]
        visible = re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()
        date_match = DEVFOLIO_DATE_RE.search(visible)
        event_date = (
            date_match.group("date")
            if date_match and date_match.group("label").casefold() == "starts"
            else None
        )
        application_open_date = (
            date_match.group("date")
            if date_match and date_match.group("label").casefold() == "opens"
            else None
        )
        live = bool(re.search(r"\blive\b", visible, re.IGNORECASE))
        format_match = re.search(r"\b(online|offline|hybrid)\b", visible, re.IGNORECASE)
        format_text = format_match.group(1).capitalize() if format_match else ""
        section = str(current.get("section") or "").casefold()
        explicit_current_signal = bool(
            re.search(r"\b(?:open|upcoming|live|apply now|remind me)\b", visible, re.IGNORECASE)
            or date_match
        )
        is_past_listing = section == "past" or bool(
            re.search(r"\b(?:ended|past|closed|participated|see projects)\b", visible, re.IGNORECASE)
        )
        if is_past_listing or (section not in {"open", "upcoming"} and not explicit_current_signal):
            return
        if section not in {"open", "upcoming"}:
            section = "upcoming" if application_open_date else "open"

        schedule_text = (
            f"Starts {event_date}" if event_date
            else f"Applications open {application_open_date}" if application_open_date
            else "Currently live; start date not shown" if live
            else "Schedule not shown"
        )
        summary_bits = [
            "Devfolio platform listing; not independently verified as an organizer page.",
            f"Listing section: {section.title()}.",
            f"Schedule/status as displayed: {schedule_text}.",
        ]
        if format_text:
            summary_bits.append(f"Format as displayed: {format_text}.")
        if visible:
            summary_bits.append(f"Listing text: {visible[:500]}.")
        summary_bits.append("Confirm eligibility, exact timing, prizes and application rules on the event page.")
        if section == "open":
            self.saw_open = True
        elif section == "upcoming":
            self.saw_upcoming = True
        self.events.append({
            "title": current["title"],
            "link": current["link"],
            "summary": " ".join(summary_bits),
            "listing_status": section,
            "event_date_text": event_date,
            "application_open_date_text": application_open_date,
            "live": live,
            "format_text": format_text,
            "source_listing": "Devfolio Open & Upcoming Hackathons",
        })

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._heading_tag == tag:
            heading = re.sub(r"\s+", " ", " ".join(self._heading_parts)).strip().casefold()
            if heading in {"open", "upcoming", "past"}:
                self._finish_current()
                self.section = heading
                if heading == "open":
                    self.saw_open = True
                elif heading == "upcoming":
                    self.saw_upcoming = True
            self._heading_tag = None
            self._heading_parts = []

        if tag != "a" or self._anchor is None:
            return
        anchor = self._anchor
        self._anchor = None
        link = normalize_http_url(urljoin("https://devfolio.co/explore/", anchor.get("href", "")))
        if not link:
            return
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        if not host.endswith(".devfolio.co") or parts.username or parts.password:
            return
        if link in self._seen_links:
            return

        anchor_parts = [re.sub(r"\s+", " ", str(value or "")).strip() for value in anchor["parts"]]
        title = next((part for part in anchor_parts if part and not self.META_TEXT.fullmatch(part)), "")
        if not title:
            title = anchor.get("aria_label") or anchor.get("title_attr") or ""
        title = re.sub(r"\s+", " ", title).strip()[:180]
        if not title:
            return

        self._finish_current()
        self._current = {
            "title": title,
            "link": link,
            "section": self.section,
            # Some cards place metadata inside the title link; retain all later text nodes.
            "parts": anchor_parts[1:],
        }
        self._seen_links.add(link)

    def close(self) -> None:
        super().close()
        self._finish_current()


NSP_DEADLINE_RE = re.compile(
    r"Student\s+Application\s+Open\s+till(?:\s*\(for\s+Renewal\))?\s*:?\s*(\d{2}-\d{2}-\d{4})",
    re.IGNORECASE,
)


class NSPScholarshipListParser(HTMLParser):
    """Extract titled schemes and their specific Specifications/FAQ links from NSP."""

    GENERAL_HEADINGS = {"students", "schemes on nsp", "public", "institutes", "officers"}
    SCHEME_TITLE_RE = re.compile(
        r"\b(?:scholarship|scholarships|fellowship|fellowships|stipend|"
        r"financial assistance|financial support|free coaching|education grant)\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._current: dict | None = None
        self._anchor: dict | None = None
        self.entries: list[dict] = []
        self.saw_scheme_heading = False
        self._seen_detail_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): (value or "").strip() for key, value in attrs}
        tag = tag.lower()
        if tag in {"h3", "h4", "h5", "h6"}:
            self._heading_tag = tag
            self._heading_parts = []
        if tag == "a":
            self._anchor = {"href": attrs_dict.get("href", ""), "parts": []}

    def handle_data(self, data: str) -> None:
        if self._heading_tag:
            self._heading_parts.append(data)
        elif self._current is not None and self._anchor is None:
            self._current["text_parts"].append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def _finish_current(self) -> None:
        current = self._current
        self._current = None
        if not current:
            return
        detail_url = current.get("specifications_url") or current.get("faq_url")
        if not detail_url or detail_url in self._seen_detail_links:
            return
        listing_text = re.sub(
            r"\s+", " ", " ".join(str(part or "") for part in current.get("text_parts", []))
        ).strip()
        raw_deadline_match = NSP_DEADLINE_RE.search(listing_text)
        raw_deadline = raw_deadline_match.group(1) if raw_deadline_match else None
        display_deadline = None
        if raw_deadline:
            try:
                parsed = datetime.strptime(raw_deadline, "%d-%m-%Y")
                display_deadline = parsed.strftime("%B %d, %Y")
            except ValueError:
                display_deadline = None

        deadline_text = (
            f"Student application deadline: {display_deadline} (listed as {raw_deadline}; unverified against scheme details)."
            if display_deadline
            else "Student application deadline was not parsed from the current listing; verify on the portal."
        )
        specification_url = current.get("specifications_url")
        faq_url = current.get("faq_url")
        summary_bits = [
            "National Scholarship Portal (NSP), academic year 2026-27; official listing, scheme details not independently verified.",
            deadline_text,
            "Official portal listing: https://scholarships.gov.in/All-Scholarships.",
        ]
        if specification_url:
            summary_bits.append(f"Specifications document: {specification_url}.")
        if faq_url:
            summary_bits.append(f"FAQ: {faq_url}.")
        summary_bits.append("Check the official portal for current application status, eligibility, required documents and applicable scheme rules.")
        self.entries.append({
            "title": current["title"],
            "link": detail_url,
            "summary": " ".join(summary_bits),
            "deadline_raw": raw_deadline,
            "deadline_display": display_deadline,
            "listing_text": listing_text[:1200],
            "academic_year": "2026-27",
            "source_listing_url": "https://scholarships.gov.in/All-Scholarships",
            "specifications_url": specification_url,
            "faq_url": faq_url,
            "source_listing": "National Scholarship Portal",
        })
        self._seen_detail_links.add(detail_url)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._heading_tag == tag:
            heading = re.sub(r"\s+", " ", " ".join(self._heading_parts)).strip()
            self._heading_tag = None
            self._heading_parts = []
            heading_lower = heading.casefold()
            looks_like_scheme = (
                len(heading) >= 14
                and heading_lower not in self.GENERAL_HEADINGS
                and not re.match(r"^academic\s+year\b", heading_lower)
                and bool(self.SCHEME_TITLE_RE.search(heading))
            )
            if looks_like_scheme:
                self._finish_current()
                self._current = {
                    "title": heading,
                    "text_parts": [],
                    "specifications_url": None,
                    "faq_url": None,
                }
                self.saw_scheme_heading = True
            elif self._current and heading:
                self._current["text_parts"].append(heading)

        if tag == "a" and self._anchor is not None:
            anchor = self._anchor
            self._anchor = None
            if not self._current:
                return
            label = re.sub(r"\s+", " ", " ".join(anchor["parts"])).strip().casefold()
            url = normalize_http_url(urljoin("https://scholarships.gov.in/All-Scholarships", anchor.get("href", "")))
            if not url:
                return
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            if host not in {"scholarships.gov.in", "www.scholarships.gov.in"}:
                return
            if parts.path.rstrip("/").casefold() in {"/all-scholarships", "/students"} and not parts.query:
                return
            if "specification" in label:
                self._current["specifications_url"] = url
            elif "faq" in label:
                self._current["faq_url"] = url

def _resolve_source_type(url: str) -> tuple[str, str]:
    host = _host(url)
    parts = urlsplit(url)
    if host in {"hackalendar.com", "www.hackalendar.com"} and parts.path.rstrip("/") == "/feed.xml":
        return "hackalendar_rss", "Hackalendar · Upcoming Hackathons"
    if host in {"devfolio.co", "www.devfolio.co"} and parts.path.rstrip("/").lower() == "/explore":
        return "devfolio_html", "Devfolio · Open & Upcoming Hackathons"
    if host in {"unstop.com", "www.unstop.com"} and re.fullmatch(
        r"/(?:compete|hackathons|competitions|internship-portal|scholarships)/amp", parts.path.rstrip("/"), re.IGNORECASE
    ):
        path = parts.path.rstrip("/").casefold()
        if path == "/hackathons/amp":
            label = "Unstop · Hackathons"
        elif path == "/internship-portal/amp":
            label = "Unstop · Internships"
        elif path == "/competitions/amp":
            label = "Unstop · Competitions"
        elif path == "/scholarships/amp":
            label = "Unstop · Scholarships"
        else:
            label = "Unstop · Open Opportunities"
        return "unstop_html", label
    if host in {"scholarships.gov.in", "www.scholarships.gov.in"} and parts.path.rstrip("/").lower() in {"/all-scholarships", "/students"}:
        return "nsp_scholarships_html", "National Scholarship Portal · Schemes"
    if host in {"mlh.com", "www.mlh.com"} and (
        parts.path.rstrip("/") == "/events"
        or re.fullmatch(r"/seasons/\d{4}/events", parts.path.rstrip("/"))
    ):
        return "mlh_events_html", "MLH · Upcoming Events Calendar"
    if host == "news.google.com" and parts.path.startswith("/rss/"):
        query = (parse_qs(parts.query).get("q") or [""])[0].lower()
        if "devpost.com" in query:
            label = "Google News · Devpost"
        elif "mlh.io" in query:
            label = "Google News · MLH"
        elif "scholarships.gov.in" in query:
            label = "Google News · National Scholarship Portal"
        elif "blog.google" in query and "student-programs" in query:
            label = "Google News · Google Student Programs"
        elif "travel grant" in query or "fully funded" in query:
            label = "Google News · Funded travel"
        elif "scholarship" in query:
            label = "Google News · Scholarships"
        else:
            label = "Google News RSS"
        return "google_news_rss", label
    if host in {"github.blog", "www.github.blog"} and parts.path.endswith("/feed/"):
        return "official_blog_rss", "GitHub Blog RSS"
    return "rss_atom", f"RSS/Atom · {host or 'unknown host'}"


def build_source_config(url: str) -> SourceConfig:
    source_url = str(url or "").strip()
    adapter_type, name = _resolve_source_type(source_url)
    source_id = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    if adapter_type == "google_news_rss":
        expected_fields = ("title", "link", "summary", "published", "source", "media")
        pagination_mode = "google_news_feed_managed_recent_results"
        access_method = "public_google_news_rss"
    elif adapter_type == "hackalendar_rss":
        expected_fields = ("title", "link", "summary", "description", "published", "updated", "category", "guid")
        pagination_mode = "catalogue_feed_upcoming_events"
        access_method = "public_hackalendar_rss"
    elif adapter_type == "official_blog_rss":
        expected_fields = ("title", "link", "summary", "published", "updated", "author", "media")
        pagination_mode = "publisher_feed_managed"
        access_method = "public_official_blog_rss"
    elif adapter_type == "mlh_events_html":
        expected_fields = ("title", "link", "summary", "event_date_text", "calendar_year", "location_text")
        pagination_mode = "official_calendar_upcoming_section"
        access_method = "public_official_mlh_events_html"
    elif adapter_type == "devfolio_html":
        expected_fields = ("title", "link", "summary", "listing_status", "event_date_text", "application_open_date_text", "format_text")
        pagination_mode = "platform_open_upcoming_sections"
        access_method = "public_devfolio_explore_html"
    elif adapter_type == "unstop_html":
        expected_fields = ("title", "link", "summary", "listing_category", "listing_countdown_text", "source_listing")
        pagination_mode = "current_unstop_listing_cards"
        access_method = "public_unstop_html_listing"
    elif adapter_type == "nsp_scholarships_html":
        expected_fields = ("title", "link", "summary", "deadline_raw", "deadline_display", "specifications_url", "faq_url")
        pagination_mode = "portal_scheme_list_current_year"
        access_method = "public_official_nsp_scholarship_html"
    else:
        expected_fields = ("title", "link", "summary", "description", "published", "updated", "media")
        pagination_mode = "publisher_feed_managed"
        access_method = "public_rss_or_atom"
    return SourceConfig(
        source_id=source_id,
        name=name,
        url=source_url,
        adapter_type=adapter_type,
        access_method=access_method,
        expected_fields=expected_fields,
        pagination_mode=pagination_mode,
    )


class RSSSourceAdapter:
    """Adapter interface shared by RSS/Atom sources and specialized link resolvers."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    def fetch(
        self,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
    ) -> FeedResult:
        started = time.monotonic()
        fetched_at = utc_now()
        response = session.get(
            self.config.url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            error = getattr(parsed, "bozo_exception", None)
            raise RuntimeError(f"Invalid RSS/Atom feed ({type(error).__name__ if error else 'parse error'})")
        entries = list(parsed.entries)
        return FeedResult(
            entries=entries,
            status="success",
            item_count=len(entries),
            duration_ms=round((time.monotonic() - started) * 1000),
            fetched_at_utc=fetched_at,
        )

    def resolve_item_url(
        self,
        item_url: str,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
    ) -> tuple[str | None, str]:
        """Return a normalized direct link; specialized adapters may override this."""
        normalized = normalize_http_url(item_url)
        return (normalized, "direct_link") if normalized else (None, "invalid_url")


class GoogleNewsRSSAdapter(RSSSourceAdapter):
    """Resolve modern Google News wrapper URLs where possible."""

    def resolve_item_url(
        self,
        item_url: str,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
    ) -> tuple[str | None, str]:
        normalized = normalize_http_url(item_url)
        if not normalized:
            return None, "invalid_url"

        # Modern Google News article IDs no longer reliably contain a decodable
        # publisher URL. Ordinary requests also often stay on news.google.com.
        # Use the MIT-licensed decoder when installed, then fall back to redirect
        # and canonical-tag checks. Resolution never confirms official status.
        if _decode_google_news is not None and session is requests:
            try:
                decoded = _decode_google_news(normalized, timeout=timeout)
                if isinstance(decoded, dict) and decoded.get("success"):
                    resolved = normalize_http_url(decoded.get("decoded_url"))
                    if resolved and _host(resolved) not in {"news.google.com", "www.google.com"}:
                        return resolved, "decoder_resolved"
                    print("Google News decoder returned a success response without a safe publisher URL.")
                elif isinstance(decoded, dict):
                    # Keep the stable resolution status for retry/backoff logic, but
                    # expose a short diagnostic so CI logs show why the decoder failed.
                    detail = re.sub(r"\s+", " ", str(decoded.get("message") or "no reason provided")).strip()
                    print(f"Google News decoder did not resolve an item: {detail[:180]}")
                else:
                    print("Google News decoder returned an unexpected response type.")
            except Exception as exc:
                # Do not silently discard decoder failures; they explain why the
                # fallback often remains on the Google wrapper in hosted runners.
                print(f"Google News decoder exception: {type(exc).__name__}")

        response = None
        try:
            response = session.get(
                normalized,
                allow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": user_agent},
                stream=True,
            )
            final_url = normalize_http_url(getattr(response, "url", "") or "")
            if final_url and _host(final_url) not in {"news.google.com", "www.google.com"}:
                return final_url, "redirect_resolved"

            parser = CanonicalLinkParser()
            prefix = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                prefix.extend(chunk)
                if b"</head" in prefix.lower() or len(prefix) >= 128_000:
                    break
            encoding = getattr(response, "encoding", None) or "utf-8"
            parser.feed(bytes(prefix).decode(encoding, errors="replace"))
            for candidate in parser.urls:
                cleaned = normalize_http_url(candidate)
                if cleaned and _host(cleaned) not in {"news.google.com", "www.google.com"}:
                    return cleaned, "canonical_tag_resolved"
            return None, "unresolved_google_news_link"
        except requests.RequestException as exc:
            return None, f"resolution_error_{type(exc).__name__.lower()}"
        except (ValueError, UnicodeError):
            return None, "resolution_error_parse"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class HackalendarRSSAdapter(RSSSourceAdapter):
    """Public, curated hackathon discovery feed; organizer claims still require verification."""


class OfficialBlogRSSAdapter(RSSSourceAdapter):
    """RSS adapter for the configured official GitHub Blog feed."""




class MLHEventsHTMLAdapter(RSSSourceAdapter):
    """Fetch the public official MLH events calendar without authentication."""

    MAX_HTML_BYTES = 3_000_000

    def fetch(
        self,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
    ) -> FeedResult:
        started = time.monotonic()
        fetched_at = utc_now()
        response = session.get(
            self.config.url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.MAX_HTML_BYTES:
                    raise RuntimeError("MLH calendar HTML exceeded the 3 MB safety limit")
                chunks.append(chunk)
            page_html = b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            parser = MLHUpcomingEventsParser()
            parser.feed(page_html)
            if not parser.saw_upcoming_heading:
                raise RuntimeError("MLH calendar changed: Upcoming Events heading was not found")
            if not parser.events:
                raise RuntimeError("MLH calendar parser found no dated organizer links in Upcoming Events")
            return FeedResult(
                entries=parser.events,
                status="success",
                item_count=len(parser.events),
                duration_ms=round((time.monotonic() - started) * 1000),
                fetched_at_utc=fetched_at,
            )
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                closer()



def nsp_html_diagnostic(page_html: str) -> str:
    """Summarize public-page markup around expected NSP labels when detection fails."""
    body = str(page_html or "")
    lowered = body.casefold()
    tag_counts = {
        tag: len(re.findall(r"<" + tag + r"\b", body, re.IGNORECASE))
        for tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "span", "strong", "a")
    }
    snippets = []
    for term in ("Academic Year", "AICTE", "Student Application Open till", "Specifications", "FAQ"):
        index = lowered.find(term.casefold())
        if index >= 0:
            snippet = re.sub(r"\s+", " ", body[max(0, index - 100):index + 220])
            snippets.append(f"{term}={snippet[:260]}")
    return f"html_bytes={len(body)}; tag_counts={tag_counts}; snippets={snippets}"


UNSTOP_DETAIL_PATH_RE = re.compile(
    r"^/(?P<kind>hackathons|competitions|scholarships|internships|challenges|fellowships|jobs)/[^/]+-\d{5,}$",
    re.IGNORECASE,
)
UNSTOP_EXPIRED_RE = re.compile(
    r"\b(?:expired|registration\s+closed|applications?\s+closed|event\s+ended)\b",
    re.IGNORECASE,
)
UNSTOP_COUNTDOWN_RE = re.compile(r"\b\d+\s+(?:days?|hours?)\s+left\b", re.IGNORECASE)
UNSTOP_CARD_TAIL_RE = re.compile(
    r"\s+\d[\d,]*\s+(?:applied|registered|participants?)\b"
    r"|\s+\d+\s+(?:days?|hours?)\s+left\b"
    r"|\s+posted\s+\d{1,2}\s+[a-z]{3,9}\b",
    re.IGNORECASE,
)


class UnstopExploreParser(HTMLParser):
    """Extract direct opportunity-detail links from Unstop's public listing pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._anchor: dict | None = None
        self.events: list[dict] = []
        self._seen_links: set[str] = set()
        self.anchor_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): (value or "").strip() for key, value in attrs}
        self._anchor = {
            "href": attrs_dict.get("href", ""),
            "aria_label": attrs_dict.get("aria-label", ""),
            "title_attr": attrs_dict.get("title", ""),
            "parts": [],
        }
        self.anchor_count += 1

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._anchor is None:
            return
        anchor = self._anchor
        self._anchor = None
        link = normalize_http_url(urljoin("https://unstop.com/compete/amp", anchor.get("href", "")))
        if not link:
            return
        parts = urlsplit(link)
        host = (parts.hostname or "").lower()
        path_match = UNSTOP_DETAIL_PATH_RE.fullmatch(parts.path)
        if host not in {"unstop.com", "www.unstop.com"} or not path_match:
            return
        if parts.username or parts.password or link in self._seen_links:
            return

        visible = re.sub(r"\s+", " ", " ".join(str(v or "") for v in anchor["parts"])).strip()
        if not visible:
            visible = str(anchor.get("aria_label") or anchor.get("title_attr") or "").strip()
        if not visible or UNSTOP_EXPIRED_RE.search(visible):
            return

        kind = path_match.group("kind").casefold()
        raw_visible = visible
        visible = re.sub(
            r"^(?:hackathons|competitions|scholarships|internships|challenges|fellowships|jobs)\s+",
            "", visible, flags=re.IGNORECASE,
        )
        # Unstop prefixes cards with display chips such as "Online Free",
        # "Festival" or "Launching soon". Remove those from the title only; preserve the exact
        # listing text below as evidence and never derive a date from countdowns.
        visible = re.sub(
            r"^(?:(?:online|offline|hybrid)\s+free\s+|(?:online|offline|hybrid)\s+|festival\s+|(?:launching|closing|starting)\s+soon\s+)",
            "", visible, flags=re.IGNORECASE,
        )
        mode_match = re.match(
            r"^(?P<organizer>.+?)\s+(?:online|offline|hybrid)\s+(?P<remainder>.+)$",
            visible, re.IGNORECASE,
        )
        if mode_match:
            organizer = mode_match.group("organizer").strip()
            remainder = mode_match.group("remainder").strip()
            if remainder.casefold().startswith(organizer.casefold() + " "):
                visible = remainder
        tail = UNSTOP_CARD_TAIL_RE.search(visible)
        title = visible[:tail.start()] if tail else visible
        title = re.sub(r"\s+", " ", title).strip(" \t-|:·")[:180]
        if not title or title.casefold() in {"view all", "browse hackathons", "browse competitions"}:
            return

        countdown = UNSTOP_COUNTDOWN_RE.search(raw_visible)
        countdown_text = countdown.group(0) if countdown else None
        listing_text = raw_visible[:700]
        self.events.append({
            "title": title,
            "link": link,
            "summary": (
                f"Unstop public platform listing ({kind}). Listing text: {listing_text}. "
                "This is discovery evidence only; verify eligibility, dates, rewards and organizer rules "
                "on the linked opportunity page."
            ),
            "listing_category": kind,
            "listing_countdown_text": countdown_text,
            "source_listing": "Unstop Open Opportunities",
        })
        self._seen_links.add(link)


def unstop_html_diagnostic(page_html: str, parser: UnstopExploreParser) -> str:
    """Give maintainers compact clues when Unstop changes its listing markup."""
    body = str(page_html or "")
    detail_paths = len(re.findall(
        r"/(?:hackathons|competitions|scholarships|internships|challenges|fellowships|jobs)/[^\s\"'<>]+-\d{5,}",
        body,
        re.IGNORECASE,
    ))
    return (
        f"html_bytes={len(body)}; anchors={parser.anchor_count}; "
        f"opportunity_link_candidates={detail_paths}; "
        f"has_cookie_notice={bool(re.search(r'cookies? disabled', body, re.IGNORECASE))}"
    )


class DevfolioExploreHTMLAdapter(RSSSourceAdapter):
    """Parse Devfolio's current Open and Upcoming hackathon cards."""

    MAX_HTML_BYTES = 4_000_000

    def fetch(
        self,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
    ) -> FeedResult:
        started = time.monotonic()
        fetched_at = utc_now()
        response = session.get(
            self.config.url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.MAX_HTML_BYTES:
                    raise RuntimeError("Devfolio explore HTML exceeded the 4 MB safety limit")
                chunks.append(chunk)
            page_html = b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            parser = DevfolioExploreParser()
            parser.feed(page_html)
            parser.close()
            if not parser.events:
                raise RuntimeError(
                    "Devfolio parser found no current event cards with Open/Upcoming/Live/date markers"
                )
            return FeedResult(
                entries=parser.events,
                status="success",
                item_count=len(parser.events),
                duration_ms=round((time.monotonic() - started) * 1000),
                fetched_at_utc=fetched_at,
            )
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                closer()


class UnstopExploreHTMLAdapter(RSSSourceAdapter):
    """Fetch current Unstop listing cards; event pages still require separate verification."""

    MAX_HTML_BYTES = 4_000_000

    def fetch(
        self,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful portal polling)",
        session: Any = requests,
    ) -> FeedResult:
        started = time.monotonic()
        fetched_at = utc_now()
        response = session.get(
            self.config.url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.MAX_HTML_BYTES:
                    raise RuntimeError("Unstop listing HTML exceeded the 4 MB safety limit")
                chunks.append(chunk)
            page_html = b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            parser = UnstopExploreParser()
            parser.feed(page_html)
            parser.close()
            if not parser.events:
                raise RuntimeError(
                    "Unstop parser found no opportunity detail cards; " + unstop_html_diagnostic(page_html, parser)
                )
            return FeedResult(
                entries=parser.events,
                status="success",
                item_count=len(parser.events),
                duration_ms=round((time.monotonic() - started) * 1000),
                fetched_at_utc=fetched_at,
            )
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                closer()


class NSPScholarshipHTMLAdapter(RSSSourceAdapter):
    """Parse official NSP scholarship schemes and their specific Specifications/FAQ links."""

    MAX_HTML_BYTES = 4_000_000

    def fetch(
        self,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful portal polling)",
        session: Any = requests,
    ) -> FeedResult:
        started = time.monotonic()
        fetched_at = utc_now()
        response = session.get(
            self.config.url,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.MAX_HTML_BYTES:
                    raise RuntimeError("NSP scholarship listing exceeded the 4 MB safety limit")
                chunks.append(chunk)
            page_html = b"".join(chunks).decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
            parser = NSPScholarshipListParser()
            parser.feed(page_html)
            parser.close()
            parser._finish_current()
            if not parser.saw_scheme_heading:
                raise RuntimeError(
                    "NSP page changed: no scholarship-scheme title candidates were found; "
                    + nsp_html_diagnostic(page_html)
                )
            if not parser.entries:
                raise RuntimeError(
                    "NSP parser found no schemes with specific official guidance links; "
                    + nsp_html_diagnostic(page_html)
                )
            return FeedResult(
                entries=parser.entries,
                status="success",
                item_count=len(parser.entries),
                duration_ms=round((time.monotonic() - started) * 1000),
                fetched_at_utc=fetched_at,
            )
        finally:
            closer = getattr(response, "close", None)
            if callable(closer):
                closer()


class GenericRSSAdapter(RSSSourceAdapter):
    """Default adapter for other permitted RSS/Atom feeds."""


ADAPTER_REGISTRY: dict[str, type[RSSSourceAdapter]] = {
    "google_news_rss": GoogleNewsRSSAdapter,
    "hackalendar_rss": HackalendarRSSAdapter,
    "official_blog_rss": OfficialBlogRSSAdapter,
    "mlh_events_html": MLHEventsHTMLAdapter,
    "devfolio_html": DevfolioExploreHTMLAdapter,
    "unstop_html": UnstopExploreHTMLAdapter,
    "nsp_scholarships_html": NSPScholarshipHTMLAdapter,
    "rss_atom": GenericRSSAdapter,
}


def build_adapters(source_urls: list[str]) -> list[RSSSourceAdapter]:
    """Select an adapter by source type while preserving config order and uniqueness."""
    adapters: list[RSSSourceAdapter] = []
    seen: set[str] = set()
    for raw_url in source_urls:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        config = build_source_config(url)
        adapter_class = ADAPTER_REGISTRY.get(config.adapter_type, GenericRSSAdapter)
        adapters.append(adapter_class(config))
    return adapters


class URLResolutionBudget:
    """Bound costly Google News link-resolution requests across one scan.

    Legacy backfill gets its own bounded allowance so old records cannot consume
    the entire budget before newly discovered opportunities are processed.
    Direct RSS links are normalized without spending this network-request budget.
    """

    def __init__(self, max_total: int = 4, max_legacy: int = 1) -> None:
        self.max_total = max(0, int(max_total))
        self.max_legacy = max(0, min(int(max_legacy), self.max_total))
        self.used_total = 0
        self.used_legacy = 0

    def resolve(
        self,
        adapter: RSSSourceAdapter,
        item_url: str,
        *,
        timeout: int = 10,
        user_agent: str = "EventIntelligenceAgent/0.2 (personal event research; respectful feed polling)",
        session: Any = requests,
        legacy: bool = False,
    ) -> tuple[str | None, str]:
        if adapter.config.adapter_type != "google_news_rss":
            return adapter.resolve_item_url(
                item_url, timeout=timeout, user_agent=user_agent, session=session
            )
        if self.used_total >= self.max_total:
            return None, "resolution_budget_deferred"
        if legacy and self.used_legacy >= self.max_legacy:
            return None, "legacy_resolution_budget_deferred"

        self.used_total += 1
        if legacy:
            self.used_legacy += 1
        return adapter.resolve_item_url(
            item_url, timeout=timeout, user_agent=user_agent, session=session
        )


def should_poll_source(previous: dict | None, config: SourceConfig,
                      now: datetime | None = None) -> bool:
    """Enforce a minimum gap between real network attempts, including push/manual runs."""
    old = dict(previous or {})
    last_attempt = str(old.get("last_attempt_at") or "").strip()
    if not last_attempt:
        return True
    try:
        parsed = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        # An invalid legacy timestamp should not permanently prevent source polling.
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = (current - parsed.astimezone(timezone.utc)).total_seconds()
    return elapsed >= config.minimum_interval_seconds


def source_health_skipped(previous: dict | None, config: SourceConfig,
                          skipped_at: str | None = None) -> dict:
    """Record a throttled run without pretending an HTTP request or successful fetch occurred."""
    old = dict(previous or {})
    old.update({
        "source_id": config.source_id,
        "name": config.name,
        "url": config.url,
        "adapter_type": config.adapter_type,
        "access_method": config.access_method,
        "expected_fields": list(config.expected_fields),
        "pagination_mode": config.pagination_mode,
        "timeout_seconds": config.timeout_seconds,
        "minimum_interval_seconds": config.minimum_interval_seconds,
        "rate_limit_policy": config.rate_limit_policy,
        "last_status": "skipped_minimum_interval",
        "last_skipped_at": skipped_at or utc_now(),
    })
    return old


def source_health_success(previous: dict | None, config: SourceConfig, result: FeedResult,
                          matching_count: int, new_count: int) -> dict:
    """Return source health while retaining historical last-success metadata."""
    old = dict(previous or {})
    old.update({
        "source_id": config.source_id,
        "name": config.name,
        "url": config.url,
        "adapter_type": config.adapter_type,
        "access_method": config.access_method,
        "expected_fields": list(config.expected_fields),
        "pagination_mode": config.pagination_mode,
        "timeout_seconds": config.timeout_seconds,
        "minimum_interval_seconds": config.minimum_interval_seconds,
        "rate_limit_policy": config.rate_limit_policy,
        "last_attempt_at": result.fetched_at_utc,
        "last_success_at": result.fetched_at_utc,
        "last_status": "success",
        "last_error": None,
        "last_duration_ms": result.duration_ms,
        "items_seen": result.item_count,
        "matching_items": int(matching_count),
        "new_items": int(new_count),
        "last_success_items_seen": result.item_count,
        "last_success_matching_items": int(matching_count),
        "last_success_new_items": int(new_count),
        "failure_streak": 0,
    })
    return old


def source_health_failure(previous: dict | None, config: SourceConfig, error: Exception,
                          duration_ms: int, attempted_at: str | None = None) -> dict:
    """Record failure without erasing the last successful fetch timestamp."""
    old = dict(previous or {})
    old.update({
        "source_id": config.source_id,
        "name": config.name,
        "url": config.url,
        "adapter_type": config.adapter_type,
        "access_method": config.access_method,
        "expected_fields": list(config.expected_fields),
        "pagination_mode": config.pagination_mode,
        "timeout_seconds": config.timeout_seconds,
        "minimum_interval_seconds": config.minimum_interval_seconds,
        "rate_limit_policy": config.rate_limit_policy,
        "last_attempt_at": attempted_at or utc_now(),
        "last_status": "failure",
        "last_error": type(error).__name__,
        "last_duration_ms": int(duration_ms),
        "items_seen": 0,
        "matching_items": 0,
        "new_items": 0,
        "failure_streak": int(old.get("failure_streak") or 0) + 1,
    })
    return old

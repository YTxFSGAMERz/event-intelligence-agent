"""Conservative public-page verification for Event Intel.

This module extracts structured metadata and explicit text claims. It never
assumes that a reachable publisher article is the organizer's official page.
Unknown facts remain unknown, and every extracted claim records its evidence URL.
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests


MAX_HTML_BYTES = 512_000
MAX_TEXT_CHARS = 80_000
MAX_REDIRECTS = 4
DEFAULT_TIMEOUT_SECONDS = 8

_TRACKING_HOSTS = {
    "news.google.com", "google.com", "www.google.com",
    "news.ycombinator.com", "reddit.com", "www.reddit.com",
}
_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "your", "you",
    "2024", "2025", "2026", "2027", "2028", "online", "official", "apply",
    "application", "applications", "event", "program", "programme", "opportunity",
    "fully", "funded", "free", "student", "students", "international",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_safe_public_url(url: str) -> bool:
    """Allow public HTTP(S) URLs only; block local hosts, credentials and literal private IPs."""
    try:
        parts = urlsplit(str(url or "").strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return False
        if parts.username or parts.password:
            return False
        if parts.port not in (None, 80, 443):
            return False
        host = parts.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".local", ".internal", ".lan", ".home", ".test", ".invalid")
        ):
            return False
        try:
            address = ipaddress.ip_address(host.strip("[]"))
            if not address.is_global:
                return False
        except ValueError:
            # Hostnames are permitted; every redirect is validated separately.
            if "." not in host or host.startswith(".") or ".." in host:
                return False
        return True
    except (ValueError, TypeError):
        return False


def _title_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]{3,}", str(value or "").casefold())
        if token not in _STOPWORDS
    }


def title_similarity(expected: str, candidate: str) -> float:
    """Conservative lexical similarity score used only to avoid unrelated-page matches."""
    left, right = _title_tokens(expected), _title_tokens(candidate)
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    overlap_score = overlap / len(left)
    sequence_score = SequenceMatcher(None, " ".join(sorted(left)), " ".join(sorted(right))).ratio()
    return max(overlap_score, sequence_score * 0.65)


class VerificationBudget:
    """Counts page fetches (not redirect hops) to bound each workflow run."""

    def __init__(self, max_pages: int = 4) -> None:
        self.max_pages = max(0, int(max_pages))
        self.pages_used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_pages - self.pages_used)

    def consume(self) -> bool:
        if self.remaining <= 0:
            return False
        self.pages_used += 1
        return True


@dataclass
class PageFetch:
    requested_url: str
    final_url: str | None
    status: str
    http_status: int | None = None
    html: str = ""
    error: str | None = None
    duration_ms: int = 0


class PageFetchBudget:
    """Alias retained for readability in calling code."""
    pass


def fetch_public_html(
    url: str,
    *,
    budget: VerificationBudget,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_HTML_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    user_agent: str = "EventIntelligenceAgent/0.3 (+public opportunity metadata verification)",
    session=requests,
) -> PageFetch:
    requested = str(url or "").strip()
    started = time.monotonic()
    if not is_safe_public_url(requested):
        return PageFetch(requested, None, "url_rejected", error="unsafe_or_invalid_url")
    if not budget.consume():
        return PageFetch(requested, None, "skipped_budget", error="page_budget_exhausted")

    current = requested
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2"}
    for hop in range(max_redirects + 1):
        response = None
        try:
            response = session.get(
                current, allow_redirects=False, timeout=timeout, headers=headers, stream=True
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code in {301, 302, 303, 307, 308}:
                location = (getattr(response, "headers", {}) or {}).get("Location")
                response.close()
                if not location:
                    return PageFetch(requested, current, "redirect_without_location",
                                     status_code, error="redirect_location_missing",
                                     duration_ms=round((time.monotonic() - started) * 1000))
                if hop >= max_redirects:
                    return PageFetch(requested, current, "too_many_redirects",
                                     status_code, error="redirect_limit_reached",
                                     duration_ms=round((time.monotonic() - started) * 1000))
                candidate = urljoin(current, location)
                if not is_safe_public_url(candidate):
                    return PageFetch(requested, None, "redirect_rejected", status_code,
                                     error="redirect_target_rejected",
                                     duration_ms=round((time.monotonic() - started) * 1000))
                current = candidate
                continue

            if status_code < 200 or status_code >= 300:
                response.close()
                return PageFetch(requested, current, "http_error", status_code,
                                 error=f"http_{status_code}",
                                 duration_ms=round((time.monotonic() - started) * 1000))

            content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type", "")).lower()
            if content_type and not any(t in content_type for t in ("text/html", "application/xhtml+xml")):
                response.close()
                return PageFetch(requested, current, "not_html", status_code,
                                 error="content_type_not_html",
                                 duration_ms=round((time.monotonic() - started) * 1000))

            data = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                remaining = max_bytes - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
                if len(data) >= max_bytes:
                    break
            encoding = getattr(response, "encoding", None) or "utf-8"
            html = bytes(data).decode(encoding, errors="replace")
            response.close()
            return PageFetch(requested, current, "success", status_code, html=html,
                             duration_ms=round((time.monotonic() - started) * 1000))
        except requests.RequestException as exc:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            return PageFetch(requested, current, "request_error",
                             error=type(exc).__name__,
                             duration_ms=round((time.monotonic() - started) * 1000))
        except (ValueError, UnicodeError) as exc:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            return PageFetch(requested, current, "parse_error",
                             error=type(exc).__name__,
                             duration_ms=round((time.monotonic() - started) * 1000))
    return PageFetch(requested, current, "too_many_redirects", error="redirect_limit_reached",
                     duration_ms=round((time.monotonic() - started) * 1000))


class EventPageParser(HTMLParser):
    """Collect a bounded set of metadata, JSON-LD, outbound links and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.metas: dict[str, str] = {}
        self.canonical_links: list[str] = []
        self.anchors: list[dict] = []
        self.jsonld_scripts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._anchor_depth = 0
        self._anchor_href = ""
        self._anchor_rel = ""
        self._anchor_text: list[str] = []
        self._script_type = ""
        self._script_parts: list[str] = []
        self._skip_text = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "").strip() for key, value in attrs}
        name = tag.lower()
        if name == "title":
            self._in_title = True
        if name == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").strip().lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.metas:
                self.metas[key] = unescape(content)[:2000]
        if name == "link" and "canonical" in values.get("rel", "").lower().split():
            if values.get("href"):
                self.canonical_links.append(values["href"])
        if name == "a":
            self._anchor_depth += 1
            if self._anchor_depth == 1:
                self._anchor_href = values.get("href", "")
                self._anchor_rel = values.get("rel", "")
                self._anchor_text = []
        if name == "script":
            self._script_type = values.get("type", "").lower()
            self._script_parts = []
            if "ld+json" in self._script_type:
                self._skip_text += 1
        elif name in {"style", "noscript"}:
            self._skip_text += 1
        if name in {"p", "div", "li", "br", "h1", "h2", "h3", "section", "article", "tr"} and not self._skip_text:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title":
            self._in_title = False
        if name == "a" and self._anchor_depth:
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                text = re.sub(r"\s+", " ", " ".join(self._anchor_text)).strip()
                if self._anchor_href and text:
                    self.anchors.append({
                        "href": self._anchor_href,
                        "text": text[:240],
                        "rel": self._anchor_rel[:120],
                    })
                self._anchor_href = ""
                self._anchor_text = []
        if name == "script":
            if "ld+json" in self._script_type and self._script_parts:
                self.jsonld_scripts.append("".join(self._script_parts)[:150_000])
            if "ld+json" in self._script_type and self._skip_text:
                self._skip_text -= 1
            self._script_type = ""
            self._script_parts = []
        elif name in {"style", "noscript"} and self._skip_text:
            self._skip_text -= 1
        if name in {"p", "div", "li", "br", "h1", "h2", "h3", "section", "article", "tr"} and not self._skip_text:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if "ld+json" in self._script_type:
            self._script_parts.append(data)
            return
        if self._skip_text:
            return
        if self._anchor_depth:
            self._anchor_text.append(data)
        if data.strip():
            self.text_parts.append(data[:5000])


def _flatten_jsonld(value):
    if isinstance(value, list):
        for item in value:
            yield from _flatten_jsonld(item)
    elif isinstance(value, dict):
        yield value
        for key, child in value.items():
            if key in {"@graph", "mainEntity", "subjectOf", "about", "itemListElement"}:
                yield from _flatten_jsonld(child)


def _jsonld_objects(parser: EventPageParser) -> list[dict]:
    objects: list[dict] = []
    for script in parser.jsonld_scripts[:20]:
        try:
            payload = json.loads(script)
        except (ValueError, TypeError):
            continue
        objects.extend(_flatten_jsonld(payload))
    return objects


def _types(node: dict) -> set[str]:
    value = node.get("@type", [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return set()
    return {str(item).rsplit("/", 1)[-1].casefold() for item in value}


def _name(value) -> str | None:
    if isinstance(value, dict):
        result = value.get("name")
        return str(result).strip() if result else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _url_from(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("url") or value.get("@id")
    return str(value).strip() if isinstance(value, str) and value.strip() else None


def _same_site(a: str, b: str) -> bool:
    ha, hb = _host(a), _host(b)
    return bool(ha and hb and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)))


def _event_node(objects: list[dict], expected_title: str) -> tuple[dict | None, float]:
    candidates: list[tuple[float, dict]] = []
    for node in objects:
        types = _types(node)
        if not any(t.endswith("event") or t in {"hackathon", "event"} for t in types):
            continue
        node_name = str(node.get("name") or "").strip()
        if not node_name:
            continue
        score = title_similarity(expected_title, node_name)
        if score >= 0.20:
            candidates.append((score, node))
    if not candidates:
        return None, 0.0
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1], candidates[0][0]


def _location_facts(node: dict, mode_value: str, page_text: str) -> dict:
    result = {
        "raw": None, "mode": "unknown", "venue": None, "city": None,
        "region": None, "country": None, "country_code": None, "remote_restrictions": [],
    }
    mode_lower = str(mode_value or "").casefold()
    if "onlineeventattendancemode" in mode_lower:
        result["mode"] = "remote"
    elif "mixedeventattendance" in mode_lower:
        result["mode"] = "hybrid"
    elif "offlineeventattendance" in mode_lower:
        result["mode"] = "in_person"

    location = node.get("location") if isinstance(node, dict) else None
    locations = location if isinstance(location, list) else [location]
    venue_parts: list[str] = []
    mode_seen: set[str] = set()
    for place in locations:
        if isinstance(place, str):
            if place.strip():
                venue_parts.append(place.strip())
            continue
        if not isinstance(place, dict):
            continue
        types = _types(place)
        if "virtuallocation" in types:
            mode_seen.add("remote")
        if "place" in types:
            mode_seen.add("in_person")
        place_name = _name(place)
        if place_name:
            venue_parts.append(place_name)
        address = place.get("address")
        if isinstance(address, dict):
            for field, key in (("addressLocality", "city"), ("addressRegion", "region")):
                value = address.get(field)
                if value and not result[key]:
                    result[key] = str(value).strip()
            country = address.get("addressCountry")
            country_name = _name(country) or (str(country).strip() if isinstance(country, str) else None)
            if country_name:
                result["country"] = country_name
                if len(country_name) == 2 and country_name.isalpha():
                    result["country_code"] = country_name.upper()
        elif isinstance(address, str) and address.strip():
            venue_parts.append(address.strip())
    if result["mode"] == "unknown" and len(mode_seen) == 1:
        result["mode"] = next(iter(mode_seen))
    elif "remote" in mode_seen and "in_person" in mode_seen:
        result["mode"] = "hybrid"
    if venue_parts:
        result["venue"] = " · ".join(dict.fromkeys(venue_parts))[:300]
    if result["venue"] or result["city"] or result["region"] or result["country"]:
        result["raw"] = ", ".join(x for x in [result["venue"], result["city"], result["region"], result["country"]] if x)
    return result


def _explicit_date(raw: str) -> tuple[str | None, str]:
    """Normalize only unambiguous dates containing a year; never guess a year or timezone."""
    value = re.sub(r"\s+", " ", unescape(str(raw or "")).strip()).strip(" .,:;")
    formats = ("%Y-%m-%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
               "%d %B %Y", "%d %b %Y", "%d %B, %Y", "%d %b, %Y")
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed.isoformat(), "date"
        except ValueError:
            continue
    # Date-time ISO strings may be present in structured metadata; keep their
    # original timezone rather than converting to the worker's timezone.
    try:
        parsed_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed_dt.tzinfo is not None:
            return parsed_dt.isoformat(), "datetime_with_timezone"
        return parsed_dt.isoformat(), "datetime_without_timezone"
    except ValueError:
        return None, "unknown"


_DATE_TEXT = (
    r"(?:"
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,?\s+\d{4})?"
    r"|\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}"
    r")"
)
_DEADLINE_LABEL = re.compile(
    r"(?i)(application deadline|applications? (?:close|closes|close on|due)|registration (?:deadline|closes|closes on|ends|ends on)|"
    r"submission deadline|submissions? (?:close|closes|due)|last date to apply|apply (?:by|before)|applications due|deadline)\s*[:\-]?\s*"
    + r"(.{0,50}?)(" + _DATE_TEXT + r")"
)
_ELIGIBILITY_LABEL = re.compile(
    r"(?i)(eligibility|eligible applicants|who can apply|who is eligible|eligibility requirements|applicant requirements)\s*[:\-]?\s*"
    r"(.{0,350})"
)


def _extract_deadline(text: str, parser: EventPageParser, page_url: str) -> dict | None:
    # Explicit metadata label is stronger than a generic date in prose.
    for key in ("applicationdeadline", "registrationdeadline", "applyby", "deadline"):
        value = parser.metas.get(key)
        if value:
            normalized, precision = _explicit_date(value)
            return {
                "raw": value[:160], "normalized": normalized, "precision": precision,
                "snippet": f"meta:{key}={value[:160]}", "source_url": page_url,
            }

    match = _DEADLINE_LABEL.search(text[:MAX_TEXT_CHARS])
    if not match:
        return None
    raw_date = match.group(3).strip(" .,:;")
    # Require an explicit year for a normalized application deadline.
    has_year = bool(re.search(r"\b(?:19|20|21)\d{2}\b", raw_date))
    normalized, precision = _explicit_date(raw_date) if has_year else (None, "unknown")
    start = max(0, match.start() - 100)
    end = min(len(text), match.end() + 100)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()[:280]
    return {
        "raw": raw_date[:160], "normalized": normalized, "precision": precision,
        "snippet": snippet, "source_url": page_url,
    }


def _sentence_candidates(text: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", s).strip()
        for s in re.split(r"(?<=[.!?])\s+|\n+", text[:MAX_TEXT_CHARS])
        if s.strip()
    ]


def _travel_facts(text: str, page_url: str) -> dict:
    components = {
        "flight": ("flight", "airfare", "air ticket", "air travel"),
        "transport_reimbursement": ("travel reimbursement", "transport reimbursement", "travel costs", "travel expenses", "transportation"),
        "accommodation": ("accommodation", "hotel", "lodging", "housing"),
        "meals": ("meals", "food", "catering"),
        "visa_support": ("visa support", "visa assistance", "visa fee"),
    }
    facts = {}
    sentences = _sentence_candidates(text)
    positive = re.compile(r"\b(cover(?:s|ed)?|provid(?:e|es|ed)|reimburse(?:s|d|ment)?|paid|fund(?:s|ed)?|includ(?:e|es|ed)|complimentary|will pay)\b", re.I)
    negative = re.compile(r"\b(not covered|not provided|not included|not funded|not reimbursed|no travel support|self[- ]funded|at (?:your|their|own) expense|participants? (?:must|are required to) pay)\b", re.I)
    possible = re.compile(r"\b(may cover|may provide|subject to approval|limited support|considered on a case[- ]by[- ]case basis|potential support)\b", re.I)
    for field, keywords in components.items():
        candidate = next((s for s in sentences if any(k in s.casefold() for k in keywords)), None)
        if not candidate:
            facts[field] = "unknown"
            continue
        if negative.search(candidate):
            status = "not_offered"
        elif possible.search(candidate):
            status = "possible"
        elif positive.search(candidate):
            status = "confirmed"
        else:
            status = "unknown"
        facts[field] = status
        if status != "unknown":
            facts.setdefault("evidence", {})[field] = {
                "value": status, "source_url": page_url, "snippet": candidate[:280],
                "method": "explicit_page_text",
            }
    overall = "confirmed" if any(facts.get(k) == "confirmed" for k in components) else (
        "possible" if any(facts.get(k) == "possible" for k in components) else "unknown"
    )
    facts["status"] = overall
    facts.setdefault("conditions", [])
    facts["maximum_amount"] = None
    facts["currency"] = None
    facts["evidence_url"] = page_url if facts.get("evidence") else None
    return facts


def _extract_facts(page: PageFetch, expected_title: str) -> dict:
    parser = EventPageParser()
    try:
        parser.feed(page.html)
    except Exception:
        pass
    objects = _jsonld_objects(parser)
    event_node, event_score = _event_node(objects, expected_title)
    page_title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    page_title = page_title or parser.metas.get("og:title") or parser.metas.get("twitter:title") or ""
    description = parser.metas.get("description") or parser.metas.get("og:description") or ""
    body_text = re.sub(r"\s+", " ", unescape(" ".join(parser.text_parts))).strip()[:MAX_TEXT_CHARS]
    if event_node:
        name = str(event_node.get("name") or "").strip()
        if name:
            page_title = name
        if event_node.get("description"):
            description = str(event_node["description"]).strip()[:1500]

    organizer = None
    organizer_url = None
    event_start = None
    event_end = None
    event_timezone = None
    location = None
    opportunity_status = None
    image_url = None
    structured_event = bool(event_node)
    deadline = _extract_deadline(body_text, parser, page.final_url or page.requested_url)
    field_evidence = {}

    if event_node:
        organizer_obj = event_node.get("organizer")
        if isinstance(organizer_obj, list):
            organizer_obj = organizer_obj[0] if organizer_obj else None
        organizer = _name(organizer_obj)
        organizer_url = _url_from(organizer_obj)
        event_start = str(event_node.get("startDate")).strip() if event_node.get("startDate") else None
        event_end = str(event_node.get("endDate")).strip() if event_node.get("endDate") else None
        # Timezone is populated only when encoded in the actual date string.
        for date_value in (event_start, event_end):
            if date_value and (date_value.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", date_value)):
                event_timezone = "explicit_in_source"
                break
        location = _location_facts(event_node, str(event_node.get("eventAttendanceMode") or ""), body_text)
        event_status = str(event_node.get("eventStatus") or "").casefold()
        if "eventcancelled" in event_status:
            opportunity_status = "cancelled"
        elif "eventpostponed" in event_status:
            opportunity_status = "postponed"
        elif "eventrescheduled" in event_status:
            opportunity_status = "rescheduled"
        image_value = event_node.get("image")
        if isinstance(image_value, list):
            image_value = image_value[0] if image_value else None
        image_url = _url_from(image_value) or (image_value if isinstance(image_value, str) else None)
        if event_start:
            field_evidence["event_start_at"] = {
                "value": event_start, "source_url": page.final_url, "method": "schema_org_event",
                "snippet": f"JSON-LD Event.startDate={event_start}",
            }
        if event_end:
            field_evidence["event_end_at"] = {
                "value": event_end, "source_url": page.final_url, "method": "schema_org_event",
                "snippet": f"JSON-LD Event.endDate={event_end}",
            }
        if organizer:
            field_evidence["organizer"] = {
                "value": organizer, "source_url": page.final_url, "method": "schema_org_event",
                "snippet": f"JSON-LD Event.organizer.name={organizer}",
            }
        if location and location.get("raw"):
            field_evidence["location"] = {
                "value": location, "source_url": page.final_url, "method": "schema_org_event",
                "snippet": "JSON-LD Event.location / eventAttendanceMode",
            }

    if not location:
        location = {
            "raw": None, "mode": "unknown", "venue": None, "city": None,
            "region": None, "country": None, "country_code": None, "remote_restrictions": [],
        }

    # Text-only location mode is retained as an observed claim only; never inferred
    # from the mere presence of a registration form or the phrase "join online".
    eligibility_match = _ELIGIBILITY_LABEL.search(body_text)
    eligibility = {
        "status": "found" if eligibility_match else "unknown",
        "countries": [], "education_levels": [], "study_years": [], "fields_of_study": [],
        "age_min": None, "age_max": None, "requirements": [],
        "evidence_url": page.final_url if eligibility_match else None,
    }
    if eligibility_match:
        snippet = re.sub(r"\s+", " ", eligibility_match.group(0)).strip()[:350]
        eligibility["requirements"] = [snippet]
        field_evidence["eligibility"] = {
            "value": snippet, "source_url": page.final_url,
            "method": "explicit_page_text", "snippet": snippet,
        }

    travel_support = _travel_facts(body_text, page.final_url or page.requested_url)
    for key, evidence in travel_support.get("evidence", {}).items():
        field_evidence[f"travel_support.{key}"] = evidence

    deadline_status = "unknown"
    if deadline and deadline.get("raw"):
        deadline_status = "unverified"
        field_evidence["application_deadline_raw"] = {
            "value": deadline["raw"], "source_url": page.final_url,
            "method": "explicit_deadline_label", "snippet": deadline["snippet"],
        }

    if event_score >= 0.20 and structured_event:
        page_role = "structured_event_listing"
    elif page_title and title_similarity(expected_title, page_title) >= 0.28:
        page_role = "possible_event_page"
    else:
        page_role = "discovery_or_unrelated_page"

    return {
        "page_url": page.final_url or page.requested_url,
        "page_title": page_title[:300],
        "description": description[:1500] if description else "",
        "page_role": page_role,
        "title_match_score": round(max(event_score, title_similarity(expected_title, page_title)), 3),
        "structured_event": structured_event,
        "organizer": organizer,
        "organizer_url": organizer_url,
        "event_start_at": event_start,
        "event_end_at": event_end,
        "event_timezone": event_timezone,
        "opportunity_status": opportunity_status or "unknown",
        "location": location,
        "deadline": deadline,
        "deadline_status": deadline_status,
        "eligibility": eligibility,
        "travel_support": travel_support,
        "image_url": image_url,
        "field_evidence": field_evidence,
        "anchors": parser.anchors[:300],
        "canonical_url": next((urljoin(page.final_url or page.requested_url, x)
                               for x in parser.canonical_links if x), None),
        "html_text_length": len(body_text),
    }


def _candidate_official_link(facts: dict, base_url: str) -> tuple[str | None, str | None, bool]:
    """Select a relevant external CTA; only explicit official-site labels are strong evidence."""
    explicit_patterns = re.compile(r"\b(official (?:event )?(?:website|site)|event website|organizer website|organiser website|official homepage)\b", re.I)
    action_patterns = re.compile(r"\b(apply|apply now|register|registration|application|participate|submit|join|event page|website|learn more|tickets|sign up)\b", re.I)
    choices = []
    base_host = _host(base_url)
    for anchor in facts.get("anchors", []):
        href = urljoin(base_url, str(anchor.get("href") or ""))
        if not is_safe_public_url(href):
            continue
        host = _host(href)
        if host == base_host or host.endswith("." + base_host) or base_host.endswith("." + host):
            continue
        if host in _TRACKING_HOSTS:
            continue
        label = str(anchor.get("text") or "").strip()
        if not label:
            continue
        if explicit_patterns.search(label):
            rank, explicit = 0, True
        elif action_patterns.search(label):
            rank, explicit = 1, False
        else:
            continue
        choices.append((rank, -len(_title_tokens(label)), href, label, explicit))
    if not choices:
        organizer_url = facts.get("organizer_url")
        if organizer_url and is_safe_public_url(organizer_url) and not _same_site(organizer_url, base_url):
            return organizer_url, "schema_org_organizer_url", True
        return None, None, False
    choices.sort(key=lambda item: (item[0], item[1], item[2]))
    _, _, href, label, explicit = choices[0]
    return href, f"outbound_anchor:{label[:120]}", explicit


def _page_supports_opportunity(facts: dict, expected_title: str) -> bool:
    score = float(facts.get("title_match_score") or 0)
    expected = _title_tokens(expected_title)
    page = _title_tokens(facts.get("page_title") or "")
    overlap = len(expected & page)
    return score >= 0.32 and overlap >= min(2, len(expected))


def _facts_are_official(page_facts: dict, page_url: str, candidate_reason: str | None,
                        explicit_official_anchor: bool, expected_title: str) -> bool:
    if not _page_supports_opportunity(page_facts, expected_title):
        return False
    organizer_url = page_facts.get("organizer_url")
    structured_event = bool(page_facts.get("structured_event"))
    if explicit_official_anchor:
        return True
    if structured_event and organizer_url and _same_site(organizer_url, page_url):
        return True
    if candidate_reason == "schema_org_organizer_url" and structured_event and organizer_url:
        return _same_site(organizer_url, page_url)
    return False


def _evidence_urls(*urls: str | None) -> list[str]:
    output = []
    for raw in urls:
        if not raw or not is_safe_public_url(raw):
            continue
        candidate = str(raw).strip()
        if candidate not in output:
            output.append(candidate)
    return output


def verify_opportunity_page(
    discovery_url: str,
    expected_title: str,
    *,
    budget: VerificationBudget,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session=requests,
) -> dict:
    """Verify one opportunity with bounded page fetches and structured evidence."""
    checked_at = utc_now()
    source_page = fetch_public_html(
        discovery_url, budget=budget, timeout=timeout, session=session
    )
    if source_page.status != "success":
        return {
            "status": source_page.status,
            "official_page_verified": False,
            "source_page_url": source_page.final_url or discovery_url,
            "official_url": None,
            "official_link_candidate": None,
            "official_link_reason": None,
            "last_checked_at": checked_at,
            "duration_ms": source_page.duration_ms,
            "error": source_page.error,
            "evidence_urls": _evidence_urls(source_page.final_url, discovery_url),
            "observed_facts": {},
            "fact_evidence": {},
        }

    source_facts = _extract_facts(source_page, expected_title)
    source_is_official = _facts_are_official(
        source_facts, source_page.final_url or discovery_url,
        None, False, expected_title,
    )
    selected_facts = source_facts
    official_url = source_page.final_url if source_is_official else None
    official_candidate = None
    official_reason = "same_page_schema_org_organizer" if source_is_official else None
    candidate_status = None
    duration = source_page.duration_ms
    candidate_url, candidate_reason, explicit_official = _candidate_official_link(
        source_facts, source_page.final_url or discovery_url
    )

    if not source_is_official and candidate_url and budget.remaining > 0:
        official_candidate = candidate_url
        official_reason = candidate_reason
        candidate_page = fetch_public_html(
            candidate_url, budget=budget, timeout=timeout, session=session
        )
        duration += candidate_page.duration_ms
        candidate_status = candidate_page.status
        if candidate_page.status == "success":
            candidate_facts = _extract_facts(candidate_page, expected_title)
            candidate_is_official = _facts_are_official(
                candidate_facts, candidate_page.final_url or candidate_url,
                candidate_reason, explicit_official, expected_title,
            )
            # Explicit "official website" anchors are strong evidence only when
            # the destination looks related to the opportunity, not merely reachable.
            if candidate_is_official:
                official_url = candidate_page.final_url or candidate_url
                selected_facts = candidate_facts
            else:
                official_reason = f"{candidate_reason}; destination relevance/ownership not established"

    is_official = bool(official_url)
    if is_official:
        status = "official_page_verified"
    elif official_candidate:
        status = "official_link_candidate_found" if candidate_status == "success" else "official_link_candidate_unverified"
    elif source_facts.get("structured_event"):
        status = "structured_event_source_page_checked"
    else:
        status = "source_page_checked"

    observed_facts = {
        "page_role": selected_facts.get("page_role"),
        "page_title": selected_facts.get("page_title"),
        "description": selected_facts.get("description"),
        "organizer": selected_facts.get("organizer"),
        "event_start_at": selected_facts.get("event_start_at"),
        "event_end_at": selected_facts.get("event_end_at"),
        "event_timezone": selected_facts.get("event_timezone"),
        "opportunity_status": selected_facts.get("opportunity_status"),
        "location": selected_facts.get("location"),
        "deadline": selected_facts.get("deadline"),
        "deadline_status": selected_facts.get("deadline_status"),
        "eligibility": selected_facts.get("eligibility"),
        "travel_support": selected_facts.get("travel_support"),
        "image_url": selected_facts.get("image_url"),
    }
    return {
        "status": status,
        "official_page_verified": is_official,
        "source_page_url": source_page.final_url or discovery_url,
        "official_url": official_url,
        "official_link_candidate": official_candidate,
        "official_link_reason": official_reason,
        "candidate_page_status": candidate_status,
        "page_title": selected_facts.get("page_title"),
        "last_checked_at": checked_at,
        "duration_ms": duration,
        "error": None,
        "evidence_urls": _evidence_urls(source_page.final_url, official_candidate, official_url),
        "observed_facts": observed_facts,
        "fact_evidence": selected_facts.get("field_evidence") or {},
        "verified_facts": observed_facts if is_official else {},
    }


def verification_is_due(record: dict, *, now: datetime | None = None) -> bool:
    """Recheck unverified pages weekly and failed fetches daily."""
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    last_checked = str(verification.get("last_checked_at") or "").strip()
    if not last_checked:
        return True
    try:
        parsed = datetime.fromisoformat(last_checked.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = max(0, (current - parsed.astimezone(timezone.utc)).total_seconds())
    status = str(verification.get("status") or "")
    retry_seconds = 24 * 60 * 60 if status in {
        "http_error", "request_error", "parse_error", "url_rejected", "not_html"
    } else 7 * 24 * 60 * 60
    return age_seconds >= retry_seconds

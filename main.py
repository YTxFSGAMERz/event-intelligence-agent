from __future__ import annotations

import hashlib
import json
import os
from html import unescape
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests

from page_verification import VerificationBudget, verification_is_due, verify_opportunity_page
from source_adapters import (URLResolutionBudget, build_adapters, should_poll_source,
                             source_health_failure, source_health_skipped, source_health_success)

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.txt"
STATE_FILE = ROOT / "data" / "seen_events.json"
USER_AGENT = "EventIntelligenceAgent/0.1 (personal event research; respectful feed polling)"
TIMEOUT = 10
MAX_ALERTS_PER_SOURCE_PER_RUN = 1
MAX_URL_RESOLUTIONS_PER_RUN = 4
MAX_LEGACY_URL_RESOLUTIONS_PER_RUN = 1
MAX_URL_RESOLUTION_ATTEMPTS = 3
MAX_PAGE_VERIFICATIONS_PER_RUN = 2
MAX_PAGE_FETCHES_PER_RUN = 4
TELEGRAM_MIN_INTERVAL_SECONDS = 1.1
_LAST_TELEGRAM_REQUEST = 0.0

KEYWORDS = {
    "hackathons_buildathons": ["hackathon", "hack day", "coding challenge", "buildathon"],
    "competitions_challenges": ["competition", "contest", "challenge", "olympiad", "call for entries"],
    "prizes_cash_rewards": ["cash prize", "prize pool", "prizes", "winner", "cash reward", "cash rewards",
                            "prize money", "reward"],
    "swag_gadgets": ["swag", "merchandise", "giveaway", "gadget", "laptop", "phone", "hardware", "free goodies"],
    "funded_travel_abroad": ["travel grant", "travel support", "travel stipend", "flight", "airfare",
                             "accommodation covered", "fully funded", "funded travel", "travel scholarship",
                             "exchange program", "international travel support"],
    "scholarships_fellowships": ["scholarship", "fellowship", "bursary", "stipend", "tuition waiver",
                                 "tuition fee", "financial aid", "education grant"],
    "free_tickets_registration": ["free registration", "free ticket", "no registration fee", "free entry",
                                  "early bird", "early-bird", "registration opens", "free pass"],
    "open_source_programs": ["open source", "student program", "student ambassador", "summer of code",
                             "developer student clubs", "open-source program", "mentorship program"],
    "tech_events_conferences": ["tech fest", "technology festival", "student festival", "conference",
                                "summit", "developer conference", "technology meetup"],
    "internships_training": ["internship", "internships", "apprenticeship", "trainee program",
                             "training program", "fellowship application"],
}

CATEGORY_LABELS = {
    "hackathons_buildathons": "Hackathons & Buildathons",
    "competitions_challenges": "Competitions & Challenges",
    "prizes_cash_rewards": "Cash Prizes & Rewards",
    "swag_gadgets": "Swag, Gadgets & Giveaways",
    "funded_travel_abroad": "Fully Funded Travel & Study Abroad",
    "scholarships_fellowships": "Scholarships & Fellowships",
    "free_tickets_registration": "Free Tickets & Registration",
    "open_source_programs": "Open Source & Student Programs",
    "tech_events_conferences": "Tech Events & Conferences",
    "internships_training": "Internships & Training",
}

CATEGORY_ORDER = list(CATEGORY_LABELS)

ALL_TERMS = sorted({term for terms in KEYWORDS.values() for term in terms}, key=len, reverse=True)

DEADLINE_PATTERNS = [
    r"(?:registration|applications?|submissions?|apply|early[- ]bird)[^.\n]{0,45}?"
    r"(?:deadline|close[sd]?|ends?|until|by|before|through)\s*[:\-]?\s*"
    r"([A-Z][a-z]+\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})",
    r"(?:deadline|closes?|closing date|last date to apply)\s*[:\-]?\s*"
    r"([A-Z][a-z]+\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+[A-Z][a-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})",
]
OPEN_PHRASES = ["registration open", "register now", "applications open", "apply now", "tickets available"]
CLOSED_PHRASES = ["registration closed", "applications closed", "sold out", "fully booked", "registration is over"]

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def clean_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        # Drop fragments and common tracking parameters while preserving useful query parameters.
        query_parts = []
        for item in parts.query.split("&"):
            key = item.split("=", 1)[0].lower()
            if key and not key.startswith("utm_") and key not in {"ref", "source", "fbclid", "gclid"}:
                query_parts.append(item)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                           "&".join(query_parts), ""))
    except Exception:
        return url.strip()

def event_id(url: str, title: str) -> str:
    identity = clean_url(url) or title.strip().lower()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]

def read_sources() -> list[str]:
    if not SOURCES_FILE.exists():
        return []
    return [line.strip() for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]

STATE_SCHEMA_VERSION = 1
UNKNOWN_DEADLINE_TEXT = "Not found — verify on official page"


def _http_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not candidate.startswith(("https://", "http://")):
        return None
    return candidate


def normalize_event_record(event_id_value: str, record: dict) -> dict:
    """Add canonical schema fields without inventing dates, locations, or funding facts.

    Legacy keys are retained temporarily because the Telegram sender and dashboard
    still consume them. Migration is intentionally additive and idempotent.
    """
    item = dict(record or {})
    source_url = _http_url(item.get("url"))
    discovered_url = _http_url(item.get("discovered_url")) or source_url
    parsed_host = (urlsplit(source_url).hostname or "").lower() if source_url else ""
    is_discovery_redirect = parsed_host in {"news.google.com", "www.google.com"}
    canonical_url = _http_url(item.get("canonical_url"))
    if canonical_url is None and source_url and not is_discovery_redirect:
        canonical_url = clean_url(source_url)

    legacy_deadline = str(item.get("deadline") or "").strip()
    if legacy_deadline.lower() in {"", UNKNOWN_DEADLINE_TEXT.lower(), "not found"}:
        legacy_deadline = ""
    deadline_status = item.get("deadline_status")
    if deadline_status not in {"verified", "unverified", "unknown", "not_applicable"}:
        deadline_status = "unverified" if legacy_deadline else "unknown"

    location = item.get("location")
    if not isinstance(location, dict):
        location = {
            "raw": None,
            "mode": "unknown",
            "venue": None,
            "city": None,
            "region": None,
            "country": None,
            "country_code": None,
            "remote_restrictions": [],
        }

    reward = item.get("reward")
    if not isinstance(reward, dict):
        reward = {
            "type": "unknown",
            "amount_min": None,
            "amount_max": None,
            "currency": None,
            "description": None,
            "evidence_url": None,
        }

    travel_support = item.get("travel_support")
    if not isinstance(travel_support, dict):
        travel_support = {
            "status": "unknown",
            "flight": "unknown",
            "transport_reimbursement": "unknown",
            "accommodation": "unknown",
            "meals": "unknown",
            "visa_support": "unknown",
            "maximum_amount": None,
            "currency": None,
            "conditions": [],
            "evidence_url": None,
        }

    verification = item.get("verification")
    if not isinstance(verification, dict):
        verification = {
            "status": "unverified",
            "official_url": None,
            "evidence_urls": [],
            "last_checked_at": None,
            "deadline_checked_at": None,
            "eligibility_checked_at": None,
            "funding_checked_at": None,
            "notes": ["Migrated from the legacy feed tracker; official details have not been verified."],
        }

    item.update({
        "schema_version": STATE_SCHEMA_VERSION,
        "id": str(item.get("id") or event_id_value),
        "title": str(item.get("title") or "Untitled opportunity"),
        "summary": str(item.get("summary") or ""),
        "organizer": item.get("organizer"),
        "canonical_url": canonical_url,
        "discovered_url": discovered_url,
        "source_feed_url": _http_url(item.get("source_feed_url")) or _http_url(item.get("source")),
        "application_open_at": item.get("application_open_at"),
        "application_deadline": item.get("application_deadline"),
        "application_deadline_raw": item.get("application_deadline_raw") or legacy_deadline or None,
        "deadline_status": deadline_status,
        "deadline_timezone": item.get("deadline_timezone"),
        "event_start_at": item.get("event_start_at"),
        "event_end_at": item.get("event_end_at"),
        "event_timezone": item.get("event_timezone"),
        "date_precision": item.get("date_precision") or "unknown",
        "opportunity_status": item.get("opportunity_status") or "unknown",
        "location": location,
        "eligibility": item.get("eligibility") if isinstance(item.get("eligibility"), dict) else {
            "status": "unknown",
            "countries": [],
            "education_levels": [],
            "study_years": [],
            "fields_of_study": [],
            "age_min": None,
            "age_max": None,
            "requirements": [],
            "evidence_url": None,
        },
        "reward": reward,
        "travel_support": travel_support,
        "media": item.get("media") if isinstance(item.get("media"), dict) else {
            "source_image_url": _http_url(item.get("image_url")),
            "generated_image_url": None,
        },
        "verification": verification,
        "content_hash": item.get("content_hash"),
        "last_material_change_at": item.get("last_material_change_at"),
    })
    return item


def normalize_state(state: dict) -> dict:
    """Migrate old tracking records to the additive v1 schema."""
    normalized = dict(state or {})
    seen = normalized.get("seen")
    if not isinstance(seen, dict):
        seen = {}
    normalized["seen"] = {
        str(event_id_value): normalize_event_record(str(event_id_value), record)
        for event_id_value, record in seen.items()
        if isinstance(record, dict)
    }
    normalized["source_health"] = normalized.get("source_health") if isinstance(normalized.get("source_health"), dict) else {}
    normalized["schema_version"] = STATE_SCHEMA_VERSION
    return normalized


def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("seen"), dict):
            return normalize_state(data)
    except (OSError, json.JSONDecodeError):
        pass
    return {"schema_version": STATE_SCHEMA_VERSION, "seen": {}}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_state(state)
    normalized["updated_utc"] = now_iso()
    STATE_FILE.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def matching_categories(text: str) -> list[str]:
    low = text.lower()
    found = [category for category, terms in KEYWORDS.items()
             if any(term in low for term in terms)]
    return found


def primary_category(categories: list[str], title: str = "") -> str:
    """Assign one display bucket while retaining every matching tag on the event."""
    title_low = title.lower()
    # Scholarships/fellowships should not be filed under general travel just because
    # a scholarship headline also says "fully funded".
    if ("scholarships_fellowships" in categories and any(
        term in title_low for term in ("scholarship", "fellowship", "bursary", "stipend")
    )):
        return "scholarships_fellowships"
    if ("funded_travel_abroad" in categories and any(
        term in title_low for term in ("travel grant", "travel support", "fully funded travel",
                                       "study abroad", "exchange program", "international travel")
    )):
        return "funded_travel_abroad"
    for category in CATEGORY_ORDER:
        if category in categories:
            return category
    return categories[0] if categories else "tech_events_conferences"


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " ").title())

def extract_deadline(text: str) -> str:
    for pattern in DEADLINE_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,:;")
    return "Not found — verify on official page"

def registration_status(text: str) -> tuple[str, str]:
    low = text.lower()
    # Closed phrases take precedence if a feed item mentions both old and new status.
    for phrase in CLOSED_PHRASES:
        if phrase in low:
            return "CLOSED (phrase detected)", phrase
    for phrase in OPEN_PHRASES:
        if phrase in low:
            return "OPEN (phrase detected)", phrase
    return "UNKNOWN", "No explicit status phrase in feed item"

def check_link(url: str) -> tuple[str, str]:
    if not url.startswith(("https://", "http://")):
        return "NOT CHECKED", "No valid HTTP(S) URL"
    try:
        # HEAD first; some sites reject HEAD, so retry with a small GET.
        response = requests.head(url, allow_redirects=True, timeout=TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
        if response.status_code in (403, 405, 501) or response.status_code >= 500:
            response = requests.get(url, allow_redirects=True, timeout=TIMEOUT,
                                    headers={"User-Agent": USER_AGENT}, stream=True)
            response.close()
        if 200 <= response.status_code < 400:
            return "REACHABLE", f"HTTP {response.status_code}"
        return "UNAVAILABLE", f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        return "COULD NOT VERIFY", type(exc).__name__

class PageImageParser(HTMLParser):
    """Collect social-preview and inline image URLs from a page's HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.preview_images: list[str] = []
        self.inline_images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            key = (attributes.get("property") or attributes.get("name") or
                   attributes.get("itemprop") or "").lower()
            value = attributes.get("content", "").strip()
            if value and key in {
                "og:image", "og:image:url", "twitter:image", "twitter:image:src",
                "image", "image_src",
            }:
                self.preview_images.append(value)
        elif tag.lower() == "img":
            value = (attributes.get("src") or attributes.get("data-src") or
                     attributes.get("data-original") or "").strip()
            if value:
                self.inline_images.append(value)
            srcset = attributes.get("srcset", "").strip()
            if srcset:
                first = srcset.split(",", 1)[0].strip().split()
                if first:
                    self.inline_images.append(first[0])


def normalize_image_url(value: str, base_url: str) -> str | None:
    value = unescape(str(value or "")).strip()
    if not value:
        return None
    resolved = urljoin(base_url, value)
    parts = urlsplit(resolved)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return resolved


def extract_image_from_markup(markup: str, base_url: str) -> str | None:
    parser = PageImageParser()
    try:
        parser.feed(markup or "")
    except Exception:
        pass
    for candidate in parser.preview_images + parser.inline_images:
        resolved = normalize_image_url(candidate, base_url)
        if resolved:
            return resolved
    return None


def extract_event_image(entry: dict, link: str, raw_summary: str) -> str | None:
    """Prefer images provided by the feed, then try the page's social preview."""
    for field in ("media_thumbnail", "media_content", "enclosures", "links"):
        values = entry.get(field, []) or []
        if isinstance(values, dict):
            values = [values]
        for value in values:
            if not isinstance(value, dict):
                continue
            candidate = value.get("url") or value.get("href")
            if not candidate:
                continue
            mime = str(value.get("type", "")).lower()
            medium = str(value.get("medium", "")).lower()
            rel = str(value.get("rel", "")).lower()
            if field in {"enclosures", "links"} and mime and not mime.startswith("image/"):
                continue
            if field == "links" and rel not in {"enclosure", "preview", ""} and not mime.startswith("image/"):
                continue
            if field == "media_content" and mime and not mime.startswith("image/") and medium != "image":
                continue
            resolved = normalize_image_url(str(candidate), link)
            if resolved:
                return resolved

    # Some RSS feeds place their thumbnail directly inside summary HTML.
    from_summary = extract_image_from_markup(raw_summary, link)
    if from_summary:
        return from_summary

    if not link.startswith(("https://", "http://")):
        return None
    # Last resort: inspect only the beginning of the HTML document for og:image/twitter:image.
    try:
        response = requests.get(
            link,
            allow_redirects=True,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            stream=True,
        )
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type and "html" not in content_type and "xhtml" not in content_type:
            response.close()
            return None
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            sample = b"".join(chunks)
            if b"</head" in sample.lower() or size >= 256_000:
                break
        response.close()
        html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        return extract_image_from_markup(html, response.url or link)
    except requests.RequestException:
        return None


class TelegramRateLimitError(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Telegram rate limit reached; retry after {retry_after}s")


def telegram_api_call(token: str, method: str, payload: dict, files: dict | None = None) -> dict:
    global _LAST_TELEGRAM_REQUEST
    # Keep messages to the same private chat spaced out to reduce flood-control errors.
    elapsed = time.monotonic() - _LAST_TELEGRAM_REQUEST
    wait_seconds = TELEGRAM_MIN_INTERVAL_SECONDS - elapsed
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    _LAST_TELEGRAM_REQUEST = time.monotonic()

    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if files:
            response = requests.post(endpoint, data=payload, files=files, timeout=TIMEOUT)
        else:
            response = requests.post(endpoint, json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Never include the request URL in the exception: it contains the bot token.
        raise RuntimeError(f"Telegram request failed ({type(exc).__name__})") from None

    try:
        result = response.json()
    except ValueError:
        result = {}
    if not response.ok or not result.get("ok"):
        error_code = result.get("error_code", response.status_code)
        description = result.get("description", "Telegram API did not confirm message delivery")
        if error_code == 429:
            parameters = result.get("parameters") or {}
            try:
                retry_after = max(1, int(parameters.get("retry_after", 60)))
            except (TypeError, ValueError):
                retry_after = 60
            raise TelegramRateLimitError(retry_after)
        raise RuntimeError(f"Telegram API error {error_code}: {description}")
    return result


def generate_opportunity_card(item: dict, categories: list[str], deadline: str,
                              reg_status: str, primary: str | None = None) -> bytes:
    """Create a polished, readable PNG opportunity card when no source image exists."""
    import io
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1200, 675
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)

    # Midnight gradient background with subtle grid lines.
    top = (8, 15, 35)
    bottom = (25, 31, 76)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(round(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        draw.line((0, y, width, y), fill=color)
    for x in range(28, width, 48):
        draw.line((x, 0, x, height), fill=(24, 37, 76), width=1)
    for y in range(22, height, 48):
        draw.line((0, y, width, y), fill=(24, 37, 76), width=1)

    # A quiet orbit motif adds a tech-event feel without competing with the text.
    draw.ellipse((860, 95, 1170, 405), outline=(45, 91, 155), width=3)
    draw.ellipse((915, 150, 1115, 350), outline=(41, 166, 185), width=3)
    draw.ellipse((970, 205, 1060, 295), outline=(115, 99, 223), width=3)
    draw.ellipse((1020, 185, 1040, 205), fill=(73, 226, 237))

    draw.rounded_rectangle(
        (44, 38, 1156, 637), radius=30,
        fill=(11, 20, 44), outline=(52, 79, 125), width=2,
    )
    draw.rounded_rectangle((44, 38, 58, 637), radius=7, fill=(49, 207, 222))

    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    def get_font(size: int, bold: bool = False):
        for path in (bold_paths if bold else font_paths):
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    eyebrow_font = get_font(18, True)
    title_font = get_font(42, True)
    section_font = get_font(17, True)
    value_font = get_font(22, True)
    chip_font = get_font(16, True)
    body_font = get_font(20)

    draw.rounded_rectangle((82, 70, 690, 111), radius=18, fill=(20, 54, 83))
    draw.text((101, 80), "EVENT INTELLIGENCE  /  NEW FIND", font=eyebrow_font, fill=(104, 235, 239))
    bucket = category_label(primary or primary_category(categories, item.get("title", "")))
    draw.text((82, 135), bucket.upper()[:62], font=get_font(20, True), fill=(170, 185, 222))

    title = re.sub(r"\s+", " ", item.get("title", "Untitled opportunity")).strip()
    title = title[:155]
    max_width = 940

    def wrap_lines(text: str, font, limit: int, max_lines: int) -> list[str]:
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and draw.textlength(trial, font=font) > limit:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip(" .") + "..."
        return lines

    title_lines = wrap_lines(title, title_font, max_width, 3)
    y = 184
    for line in title_lines:
        draw.text((82, y), line, font=title_font, fill=(248, 250, 255))
        y += 53

    # Category chips.
    chip_y = max(365, y + 10)
    chip_x = 82
    for label in categories[:4]:
        label_text = label.replace("_", " ").upper()
        text_width = draw.textlength(label_text, font=chip_font)
        chip_width = int(text_width + 34)
        if chip_x + chip_width > 1110:
            break
        draw.rounded_rectangle(
            (chip_x, chip_y, chip_x + chip_width, chip_y + 38),
            radius=17, fill=(25, 42, 76), outline=(55, 91, 139), width=1,
        )
        draw.text((chip_x + 17, chip_y + 9), label_text, font=chip_font, fill=(133, 219, 238))
        chip_x += chip_width + 12

    deadline_value = (deadline or "Not found — verify on official page")[:42]
    status_value = (reg_status or "UNKNOWN")[:35]
    box_y = 464
    draw.rounded_rectangle((82, box_y, 580, 579), radius=20, fill=(18, 31, 61), outline=(42, 68, 111), width=1)
    draw.rounded_rectangle((604, box_y, 1117, 579), radius=20, fill=(18, 31, 61), outline=(42, 68, 111), width=1)
    draw.text((106, box_y + 18), "DEADLINE", font=section_font, fill=(125, 164, 205))
    draw.text((106, box_y + 52), deadline_value, font=value_font, fill=(246, 248, 255))
    draw.text((630, box_y + 18), "REGISTRATION STATUS", font=section_font, fill=(125, 164, 205))
    draw.text((630, box_y + 52), status_value, font=value_font, fill=(115, 235, 192) if status_value.startswith("OPEN") else (246, 248, 255))
    draw.text((83, 598), "Check the caption for the source link and verify details with the organiser.", font=body_font, fill=(157, 174, 209))

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def telegram_send(text: str, image_url: str | None = None, caption: str | None = None, fallback_card: bytes | None = None) -> None:
    if os.getenv("DRY_RUN", "").lower() == "true":
        print("\n--- DRY RUN TELEGRAM MESSAGE ---")
        print(f"Image URL: {image_url or 'No source image; generated card fallback'}")
        print(f"Generated fallback card prepared: {'yes' if fallback_card else 'no'}")
        print(caption if (image_url or fallback_card) and caption else text)
        print("--- END ---")
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not configured; alert printed to logs instead.")
        print(f"Image URL: {image_url or 'No image found'}")
        print(text)
        return

    if image_url:
        try:
            telegram_api_call(token, "sendPhoto", {
                "chat_id": chat_id,
                "photo": image_url,
                "caption": (caption or text)[:1024],
            })
            print("Telegram photo alert sent (source image).")
            return
        except TelegramRateLimitError:
            raise
        except RuntimeError as exc:
            # A publisher's preview image may be blocked or unsupported. Use our own visual card.
            print(f"Source image delivery failed ({exc}); trying generated opportunity card.", file=sys.stderr)

    if fallback_card:
        try:
            telegram_api_call(
                token,
                "sendPhoto",
                {"chat_id": chat_id, "caption": (caption or text)[:1024]},
                files={"photo": ("event-opportunity.png", fallback_card, "image/png")},
            )
            print("Telegram photo alert sent (generated opportunity card).")
            return
        except TelegramRateLimitError:
            raise
        except RuntimeError as exc:
            print(f"Generated photo delivery failed ({exc}); falling back to text.", file=sys.stderr)

    telegram_api_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    })


def build_photo_caption(item: dict, categories: list[str], deadline: str,
                        reg_status: str, primary: str | None = None) -> str:
    """Build a compact caption that fits Telegram's 1,024-character photo-caption limit."""
    title = re.sub(r"\s+", " ", item.get("title", "Untitled event")).strip()
    summary = re.sub(r"\s+", " ", item.get("summary", "")).strip()
    url = item.get("link", "").strip()
    title = title[:180]
    categories_text = ", ".join(categories)[:150]
    deadline_text = deadline[:100]
    status_text = reg_status[:60]
    url_text = url[:400]

    bucket = category_label(primary or primary_category(categories, title))
    fixed = (
        f"🆕 {title}\n"
        f"📂 {bucket}\n"
        f"🏷️ Tags: {categories_text}\n"
        f"📅 Deadline: {deadline_text}\n"
        f"🎟️ Registration: {status_text}\n"
        f"🔗 {url_text}\n\n"
        "Verify eligibility, dates and fees on the official page."
    )
    room = max(0, 1000 - len(fixed))
    if summary and room > 20:
        short_summary = summary[:max(0, room - 14)]
        if len(summary) > len(short_summary):
            short_summary = short_summary.rstrip() + "…"
        fixed = fixed.replace(
            f"🔗 {url_text}",
            f"📝 {short_summary}\n🔗 {url_text}",
        )
    return fixed[:1024]

def build_alert(item: dict, categories: list[str], deadline: str,
                reg_status: str, reg_evidence: str, link_status: str, link_evidence: str) -> str:
    title = item.get("title", "Untitled event").strip()
    url = item.get("link", "").strip()
    summary = re.sub(r"\s+", " ", item.get("summary", "")).strip()
    if len(summary) > 500:
        summary = summary[:497] + "..."
    return (
        f"🆕 EVENT OPPORTUNITY\n\n"
        f"{title}\n"
        f"Categories: {', '.join(categories)}\n"
        f"Deadline: {deadline}\n"
        f"Registration status: {reg_status}\n"
        f"Status evidence: {reg_evidence}\n"
        f"Link check: {link_status} ({link_evidence})\n\n"
        f"Details: {summary or 'No summary supplied by feed.'}\n\n"
        f"Official/source link: {url}\n\n"
        f"First detected (UTC): {now_iso()}\n"
        f"Note: verify eligibility, dates, fees and ticket inventory on the official page."
    )

TITLE_DEDUPE_WINDOW_DAYS = 90


def normalize_title_key(value: str) -> str:
    """Normalize punctuation/case for conservative exact-title cross-source matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def find_existing_event_id(
    seen: dict,
    candidate_url: str,
    candidate_title: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Match URLs first, then conservative exact titles from the recent 90-day window."""
    target = clean_url(candidate_url)
    if target:
        for existing_id, record in seen.items():
            if not isinstance(record, dict):
                continue
            for key in ("canonical_url", "url", "discovered_url"):
                value = record.get(key)
                if value and clean_url(str(value)) == target:
                    return str(existing_id)

    title_key = normalize_title_key(candidate_title or "")
    # Short/generic titles are too collision-prone for title-based deduplication.
    if len(title_key) < 24 or len(title_key.split()) < 4:
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    for existing_id, record in seen.items():
        if not isinstance(record, dict) or normalize_title_key(record.get("title", "")) != title_key:
            continue
        first_seen = str(record.get("first_seen_utc") or "").strip()
        if not first_seen:
            continue
        try:
            parsed = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age_seconds = abs((current - parsed.astimezone(timezone.utc)).total_seconds())
        if age_seconds <= TITLE_DEDUPE_WINDOW_DAYS * 24 * 60 * 60:
            return str(existing_id)
    return None


def remember_source_alias(
    record: dict,
    source_id: str,
    source_name: str,
    source_feed_url: str,
    discovered_url: str,
    resolved_url: str | None = None,
) -> None:
    """Retain multi-source provenance whenever two feeds point to the same opportunity."""
    def add_unique(field: str, value: str | None) -> None:
        if not value:
            return
        values = record.get(field)
        if not isinstance(values, list):
            values = []
        if value not in values:
            values.append(value)
        record[field] = values

    add_unique("source_ids", str(record.get("source_id") or ""))
    add_unique("source_ids", source_id)
    add_unique("source_names", str(record.get("source_name") or record.get("source") or ""))
    add_unique("source_names", source_name)
    add_unique("source_feeds", str(record.get("source_feed_url") or record.get("source") or ""))
    add_unique("source_feeds", source_feed_url)
    add_unique("discovered_urls", str(record.get("discovered_url") or record.get("url") or ""))
    add_unique("discovered_urls", clean_url(discovered_url))
    add_unique("resolved_urls", clean_url(resolved_url) if resolved_url else None)


def apply_page_verification(record: dict, result: dict) -> dict:
    """Merge verified facts, keeping unverified page observations separate."""
    target = dict(record or {})
    old_verification = target.get("verification")
    verification = dict(old_verification) if isinstance(old_verification, dict) else {}
    prior_official_url = verification.get("official_url")
    prior_was_verified = (
        verification.get("status") == "official_page_verified" and bool(prior_official_url)
    )

    result_status = str(result.get("status") or "unknown")
    now_checked = result.get("last_checked_at")
    if result_status != "skipped_budget" and now_checked:
        verification["last_checked_at"] = now_checked
        verification["version"] = int(result.get("verification_version") or 1)
    verification["source_page_url"] = result.get("source_page_url") or verification.get("source_page_url")
    verification["official_link_candidate"] = result.get("official_link_candidate") or verification.get("official_link_candidate")
    verification["official_link_reason"] = result.get("official_link_reason") or verification.get("official_link_reason")
    verification["candidate_page_status"] = result.get("candidate_page_status") or verification.get("candidate_page_status")
    verification["candidate_page_error"] = result.get("candidate_page_error")
    verification["candidate_page_http_status"] = result.get("candidate_page_http_status")
    verification["page_title"] = result.get("page_title") or verification.get("page_title")
    verification["last_check_error"] = result.get("error")
    verification["last_check_duration_ms"] = result.get("duration_ms")
    verification["evidence_urls"] = list(dict.fromkeys(
        list(verification.get("evidence_urls") or []) + list(result.get("evidence_urls") or [])
    ))
    if result.get("observed_facts"):
        verification["observed_facts"] = result["observed_facts"]
    if result.get("fact_evidence"):
        evidence = dict(verification.get("fact_evidence") or {})
        evidence.update(result["fact_evidence"])
        verification["fact_evidence"] = evidence

    if result.get("official_page_verified") and result.get("official_url"):
        verification["status"] = "official_page_verified"
        verification["official_url"] = result["official_url"]
        verification["official_link_reason"] = result.get("official_link_reason")
        verification["last_verified_at"] = now_checked
        verified = result.get("verified_facts") or {}
        for field in ("organizer", "event_start_at", "event_end_at", "event_timezone", "opportunity_status"):
            value = verified.get(field)
            if value not in (None, "", "unknown"):
                target[field] = value
        location = verified.get("location")
        if isinstance(location, dict) and any(
            location.get(key) for key in ("raw", "venue", "city", "region", "country", "country_code")
        ):
            merged_location = dict(target.get("location") or {})
            for key, value in location.items():
                if value not in (None, "", [], "unknown"):
                    merged_location[key] = value
            target["location"] = merged_location

        deadline = verified.get("deadline")
        if isinstance(deadline, dict) and deadline.get("normalized"):
            target["application_deadline"] = deadline["normalized"]
            target["application_deadline_raw"] = deadline.get("raw")
            target["deadline_status"] = "verified"
            target["date_precision"] = deadline.get("precision") or "date"
            target["deadline_timezone"] = deadline.get("timezone")
            # Keep the dashboard and Telegram fallback compatible with the verified date.
            target["deadline"] = deadline.get("raw") or deadline["normalized"]
            verification["deadline_checked_at"] = now_checked
        elif isinstance(deadline, dict) and deadline.get("raw"):
            # Preserve an official-page claim even when there is no safe, explicit year.
            target["application_deadline_raw"] = deadline.get("raw")
            target["deadline_status"] = "unknown"
            target["application_deadline"] = None

        eligibility = verified.get("eligibility")
        if isinstance(eligibility, dict) and eligibility.get("requirements"):
            merged_eligibility = dict(target.get("eligibility") or {})
            merged_eligibility.update(eligibility)
            merged_eligibility["status"] = "verified"
            merged_eligibility["evidence_url"] = result.get("official_url")
            target["eligibility"] = merged_eligibility
            verification["eligibility_checked_at"] = now_checked

        travel = verified.get("travel_support")
        if isinstance(travel, dict) and travel.get("status") != "unknown":
            merged_travel = dict(target.get("travel_support") or {})
            merged_travel.update(travel)
            merged_travel["evidence_url"] = result.get("official_url")
            target["travel_support"] = merged_travel
            verification["funding_checked_at"] = now_checked

        registration_status = verified.get("registration_status")
        if registration_status in {"open", "closed"}:
            target["registration_status"] = (
                "OPEN (official page)" if registration_status == "open"
                else "CLOSED (official page)"
            )
            target["registration_status_evidence"] = (
                verified.get("registration_status_evidence") or "Explicit status phrase on official page."
            )

        media = dict(target.get("media") or {})
        if verified.get("image_url") and _http_url(verified["image_url"]):
            media["source_image_url"] = verified["image_url"]
            target["image_url"] = verified["image_url"]
        target["media"] = media
        target["canonical_url"] = target.get("canonical_url") or result.get("source_page_url")
        target["url"] = result["official_url"]
        target["link_status"] = target.get("link_status") or "NOT CHECKED"
    elif prior_was_verified:
        # Do not revoke a prior successful official verification just because a later
        # fetch was unavailable or an aggregator temporarily changed its markup.
        verification["status"] = "official_page_verified"
        verification["official_url"] = prior_official_url
        verification["last_recheck_status"] = result_status
        verification.setdefault("notes", []).append(
            f"Recheck on {now_checked or 'unknown time'} did not reconfirm the official page."
        )
    else:
        verification["status"] = result_status
        verification["official_url"] = None

    target["verification"] = verification
    return target


def _is_google_news_wrapper(value: object) -> bool:
    try:
        return (urlsplit(str(value or "").strip()).hostname or "").lower() in {
            "news.google.com", "www.google.com"
        }
    except ValueError:
        return False


def _verification_target(record: dict) -> str | None:
    verification = record.get("verification") if isinstance(record.get("verification"), dict) else {}
    # Once identified, the organizer's official URL is the best refresh target.
    candidates = (
        verification.get("official_url"),
        record.get("canonical_url"),
        record.get("url"),
        record.get("discovered_url"),
    )
    for candidate in candidates:
        if _http_url(candidate) and not _is_google_news_wrapper(candidate):
            return str(candidate).strip()
    return None


def verify_due_opportunities(pending_events: list[dict], seen: dict) -> int:
    """Verify at most two records and four pages per workflow run."""
    budget = VerificationBudget(max_pages=MAX_PAGE_FETCHES_PER_RUN)
    checked = 0

    # Always prioritize one newly discovered opportunity, then backfill one record.
    targets: list[tuple[str, dict, str]] = []
    if pending_events:
        first = pending_events[0]
        pending_target = first.get("canonical_link") or first.get("link") or ""
        if _is_google_news_wrapper(pending_target):
            # A Google News wrapper is a discovery pointer, not an event page.
            # Avoid spending verification requests until the source resolver finds
            # a publisher URL; the item remains eligible for a later retry.
            first["verification_result"] = {
                "status": "awaiting_source_resolution",
                "verification_version": 1,
                "official_page_verified": False,
                "source_page_url": None,
                "official_url": None,
                "official_link_candidate": None,
                "official_link_reason": None,
                "last_checked_at": None,
                "duration_ms": 0,
                "error": "Google News wrapper has not resolved to a publisher page.",
                "evidence_urls": [],
                "observed_facts": {},
                "fact_evidence": {},
                "verified_facts": {},
            }
            print(f"Page verification deferred for {first.get('title')!r}: awaiting publisher URL resolution.")
        elif pending_target:
            targets.append(("pending", first, pending_target))

    pending_ids = {str(event.get("uid") or "") for event in pending_events}
    existing_candidates = []
    for existing_id, record in seen.items():
        if not isinstance(record, dict) or str(existing_id) in pending_ids:
            continue
        if not verification_is_due(record):
            continue
        target_url = _verification_target(record)
        if not target_url:
            continue
        first_seen = str(record.get("first_seen_utc") or "")
        existing_candidates.append((first_seen, str(existing_id), record, target_url))
    existing_candidates.sort(key=lambda item: item[0], reverse=True)
    if existing_candidates and len(targets) < MAX_PAGE_VERIFICATIONS_PER_RUN:
        _, existing_id, record, target_url = existing_candidates[0]
        targets.append((existing_id, record, target_url))

    for key, record, target_url in targets[:MAX_PAGE_VERIFICATIONS_PER_RUN]:
        title = str(record.get("title") or "Untitled opportunity")
        if not target_url:
            continue
        before = budget.pages_used
        result = verify_opportunity_page(
            target_url,
            title,
            budget=budget,
            timeout=min(TIMEOUT, 8),
        )
        after = budget.pages_used
        if key == "pending":
            record["verification_result"] = result
            if result.get("official_page_verified") and result.get("official_url"):
                # Use the organizer page in the new alert, but keep feed/source URLs separately.
                record["link"] = result["official_url"]
        else:
            verified_record = apply_page_verification(record, result)
            seen[key] = verified_record
        if result.get("status") == "skipped_budget":
            print(f"Verification deferred for {title!r}: page budget exhausted.")
        else:
            checked += 1
            print(
                f"Page verification: {title!r}; status={result.get('status')}; "
                f"pages_fetched={after - before}; official_page={bool(result.get('official_page_verified'))}"
            )
    print(
        f"Verification pass complete: records_checked={checked}; "
        f"page_fetches_used={budget.pages_used}/{budget.max_pages}."
    )
    return checked


def main() -> int:
    sources = read_sources()
    state = read_state()
    seen = state.setdefault("seen", {})
    feed_errors = 0
    telegram_errors = 0
    telegram_rate_limited = False
    pending_events: list[dict] = []

    if not sources:
        print("No sources configured. Add public RSS/Atom URLs to sources.txt.")
        print("No network discovery was performed.")
        return 0

    # Discovery pass: adapters normalize fetching while source-specific behavior
    # (such as resolving Google News redirect links) stays outside the alert logic.
    source_health = state.setdefault("source_health", {})
    adapters = build_adapters(sources)
    adapters_by_url = {adapter.config.url: adapter for adapter in adapters}
    pending_url_index: dict[str, str] = {}
    resolution_budget = URLResolutionBudget(
        max_total=MAX_URL_RESOLUTIONS_PER_RUN,
        max_legacy=MAX_LEGACY_URL_RESOLUTIONS_PER_RUN,
    )

    # Backfill at most one unresolved legacy Google News link per run so that
    # historical records cannot starve URL resolution for new discoveries.
    unresolved_records = sorted(
        seen.items(),
        key=lambda pair: (
            int(pair[1].get("resolution_attempts") or 0) if isinstance(pair[1], dict) else 99,
            str(pair[1].get("first_seen_utc") or "") if isinstance(pair[1], dict) else "",
        ),
    )

    # Do not issue article-resolution HTTP requests during rapid push/manual runs
    # when every Google News feed is still inside its minimum polling interval.
    google_news_source_due = any(
        adapter.config.adapter_type == "google_news_rss"
        and should_poll_source(source_health.get(adapter.config.source_id), adapter.config)
        for adapter in adapters
    )
    if not google_news_source_due:
        unresolved_records = []
        print("Legacy URL backfill skipped: Google News feeds are inside the minimum poll interval.")
    for existing_id, existing in unresolved_records:
        if (resolution_budget.used_total >= resolution_budget.max_total
                or resolution_budget.used_legacy >= resolution_budget.max_legacy):
            break
        if not isinstance(existing, dict) or existing.get("canonical_url"):
            continue
        discovered_url = str(existing.get("discovered_url") or existing.get("url") or "").strip()
        if (urlsplit(discovered_url).hostname or "").lower() != "news.google.com":
            continue
        attempts = int(existing.get("resolution_attempts") or 0)
        if attempts >= MAX_URL_RESOLUTION_ATTEMPTS:
            continue
        source_url = str(existing.get("source_feed_url") or existing.get("source") or "").strip()
        adapter = adapters_by_url.get(source_url)
        if adapter is None or adapter.config.adapter_type != "google_news_rss":
            continue
        resolved, resolution_status = resolution_budget.resolve(
            adapter, discovered_url, timeout=adapter.config.timeout_seconds,
            user_agent=USER_AGENT, legacy=True
        )
        existing["resolution_attempts"] = attempts + 1
        existing["last_resolution_attempt_at"] = now_iso()
        existing["resolution_status"] = resolution_status
        if resolved:
            existing["canonical_url"] = clean_url(resolved)
            existing["url"] = clean_url(resolved)
            print(f"Resolved legacy Google News link for: {existing.get('title', existing_id)}")
        else:
            print(
                f"Could not resolve legacy Google News link "
                f"({existing.get('title', existing_id)}): {resolution_status}"
            )

    for adapter in adapters:
        config = adapter.config
        source = config.url
        previous_health = source_health.get(config.source_id, {})
        if not should_poll_source(previous_health, config):
            source_health[config.source_id] = source_health_skipped(previous_health, config)
            print(
                f"Source poll skipped by minimum interval: {config.name} "
                f"(last network attempt {previous_health.get('last_attempt_at', 'unknown')})"
            )
            continue
        started = time.monotonic()
        matching_count = 0
        queued_count = 0
        alerts_attempted = 0
        alert_limit_logged = False
        print(f"Checking {config.name} [{config.adapter_type}]: {source}")

        try:
            feed_result = adapter.fetch(timeout=config.timeout_seconds, user_agent=USER_AGENT)
            for entry in feed_result.entries:
                title = str(entry.get("title", "Untitled event")).strip()
                discovered_link = str(entry.get("link", "")).strip()
                if not title or not discovered_link:
                    continue

                raw_summary = str(entry.get("summary", entry.get("description", "")))
                summary = unescape(re.sub(r"<[^>]+>", " ", raw_summary))
                searchable_text = f"{title}\n{summary}"
                categories = matching_categories(searchable_text)
                if not categories:
                    continue
                matching_count += 1

                # Skip previously tracked raw links without making another network
                # request to resolve the same news redirect on every scheduled run.
                raw_uid = event_id(discovered_link, title)
                if raw_uid in seen:
                    seen[raw_uid]["last_seen_utc"] = now_iso()
                    continue
                existing_raw_id = find_existing_event_id(seen, discovered_link, title)
                if existing_raw_id is not None:
                    existing = seen[existing_raw_id]
                    existing["last_seen_utc"] = now_iso()
                    remember_source_alias(
                        existing, config.source_id, config.name, source, discovered_link
                    )
                    continue

                if alerts_attempted >= MAX_ALERTS_PER_SOURCE_PER_RUN:
                    if not alert_limit_logged:
                        print(
                            f"Per-source alert limit reached for {source}; "
                            "remaining new items will be checked on a future run."
                        )
                        alert_limit_logged = True
                    # Continue inspecting the feed so health counts and last-seen
                    # timestamps cover the full feed without queuing more alerts.
                    continue

                canonical_link, resolution_status = resolution_budget.resolve(
                    adapter, discovered_link, timeout=config.timeout_seconds, user_agent=USER_AGENT
                )
                resolved_link = canonical_link or discovered_link
                uid = event_id(resolved_link, title)

                # Dedupe by resolved URL across feeds, without assuming that the
                # publisher page is necessarily the organizer's official page.
                existing_id = find_existing_event_id(seen, resolved_link, title)
                if existing_id is not None:
                    existing = seen[existing_id]
                    existing["last_seen_utc"] = now_iso()
                    remember_source_alias(
                        existing, config.source_id, config.name, source,
                        discovered_link, canonical_link,
                    )
                    if canonical_link and not existing.get("canonical_url"):
                        existing["canonical_url"] = clean_url(canonical_link)
                    if discovered_link and not existing.get("discovered_url"):
                        existing["discovered_url"] = clean_url(discovered_link)
                    continue

                normalized_canonical = clean_url(canonical_link) if canonical_link else ""
                if normalized_canonical and normalized_canonical in pending_url_index:
                    print(f"Duplicate canonical URL in this scan; skipped: {title}")
                    continue

                primary = primary_category(categories, title)
                pending_events.append({
                    "uid": uid,
                    "title": title,
                    "link": resolved_link,
                    "discovered_link": discovered_link,
                    "canonical_link": canonical_link,
                    "resolution_status": resolution_status,
                    "summary": summary,
                    "raw_summary": raw_summary,
                    "entry": dict(entry),
                    "categories": categories,
                    "primary_category": primary,
                    "source": source,
                    "source_id": config.source_id,
                    "source_name": config.name,
                    "adapter_type": config.adapter_type,
                })
                if normalized_canonical:
                    pending_url_index[normalized_canonical] = uid
                alerts_attempted += 1
                queued_count += 1

            source_health[config.source_id] = source_health_success(
                previous_health, config, feed_result, matching_count, queued_count
            )
            print(
                f"Source health: success; entries={feed_result.item_count}; "
                f"matching={matching_count}; queued={queued_count}; "
                f"duration={feed_result.duration_ms}ms"
            )
        except Exception as exc:
            feed_errors += 1
            source_health[config.source_id] = source_health_failure(
                previous_health, config, exc, round((time.monotonic() - started) * 1000)
            )
            print(f"Feed error ({source}): {type(exc).__name__}: {exc}", file=sys.stderr)

    # Verify one new opportunity and backfill one older record per run.
    # Page fetches are bounded separately so a feed burst cannot cause a crawl burst.
    verify_due_opportunities(pending_events, seen)

    # One item appears once in the stream, under a primary bucket, with all tags
    # preserved on the photo card and caption.
    pending_events.sort(key=lambda event: (
        CATEGORY_ORDER.index(event["primary_category"])
        if event["primary_category"] in CATEGORY_ORDER else len(CATEGORY_ORDER),
        event["title"].casefold(),
    ))

    # Digest header gives a quick category-by-category inventory for this scan.
    if pending_events:
        category_counts: dict[str, int] = {}
        for event in pending_events:
            key = event["primary_category"]
            category_counts[key] = category_counts.get(key, 0) + 1
        digest_lines = [
            f"🧭 OPPORTUNITY DIGEST  •  {datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')}",
            f"New opportunities found: {len(pending_events)}",
            "",
        ]
        for category in CATEGORY_ORDER:
            if category_counts.get(category):
                digest_lines.append(
                    f"• {category_label(category)}: {category_counts[category]}"
                )
        try:
            telegram_send("\n".join(digest_lines))
        except TelegramRateLimitError as exc:
            telegram_errors += 1
            telegram_rate_limited = True
            print(
                f"Telegram rate limit reached while sending category digest; "
                f"retry after {exc.retry_after}s. Event cards deferred.",
                file=sys.stderr,
            )
        except Exception as exc:
            telegram_errors += 1
            print(f"Telegram digest error: {type(exc).__name__}: {exc}", file=sys.stderr)

    discovered = 0
    current_category: str | None = None
    if not telegram_rate_limited:
        for event in pending_events:
            primary = event["primary_category"]
            if primary != current_category:
                group_count = sum(1 for candidate in pending_events
                                  if candidate["primary_category"] == primary)
                try:
                    telegram_send(
                        f"📂 {category_label(primary).upper()}  •  {group_count} "
                        f"{'opportunity' if group_count == 1 else 'opportunities'}"
                    )
                except TelegramRateLimitError as exc:
                    telegram_errors += 1
                    telegram_rate_limited = True
                    print(
                        f"Telegram rate limit reached at category {category_label(primary)}; "
                        f"retry after {exc.retry_after}s. Remaining categories deferred.",
                        file=sys.stderr,
                    )
                    break
                except Exception as exc:
                    telegram_errors += 1
                    print(
                        f"Telegram category heading error ({category_label(primary)}): "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                current_category = primary

            title = event["title"]
            link = event["link"]
            categories = event["categories"]
            item = {"title": title, "link": link, "summary": event["summary"]}

            try:
                image_url = extract_event_image(event["entry"], link, event["raw_summary"])
            except Exception as exc:
                image_url = None
                print(
                    f"Image lookup failed for {title!r} ({type(exc).__name__}); "
                    "a generated card will be used.",
                    file=sys.stderr,
                )

            deadline = extract_deadline(f"{title}\n{event['summary']}")
            reg_status, reg_evidence = registration_status(f"{title}\n{event['summary']}")
            verification_result = event.get("verification_result") or {}
            verified_facts = verification_result.get("verified_facts") or {}
            if verification_result.get("official_page_verified"):
                official_deadline = verified_facts.get("deadline")
                if isinstance(official_deadline, dict) and official_deadline.get("normalized"):
                    deadline = official_deadline.get("raw") or official_deadline["normalized"]
                official_registration = verified_facts.get("registration_status")
                if official_registration in {"open", "closed"}:
                    reg_status = "OPEN (official page)" if official_registration == "open" else "CLOSED (official page)"
                    reg_evidence = verified_facts.get("registration_status_evidence") or "Explicit status phrase on official page."
            link_status, link_evidence = check_link(link)
            alert = build_alert(
                item, categories, deadline, reg_status, reg_evidence, link_status, link_evidence
            )
            photo_caption = build_photo_caption(
                item, categories, deadline, reg_status, primary=primary
            )
            try:
                fallback_card = generate_opportunity_card(
                    item, categories, deadline, reg_status, primary=primary
                )
            except Exception as exc:
                print(
                    f"Could not generate a fallback opportunity card ({type(exc).__name__}); "
                    "text will be used only if no source image is available.",
                    file=sys.stderr,
                )
                fallback_card = None

            try:
                telegram_send(
                    alert, image_url=image_url, caption=photo_caption, fallback_card=fallback_card
                )
            except TelegramRateLimitError as exc:
                telegram_errors += 1
                telegram_rate_limited = True
                print(
                    f"Telegram rate limit reached while sending {title!r}; "
                    f"retry after {exc.retry_after}s. This and remaining opportunities will retry later.",
                    file=sys.stderr,
                )
                break
            except Exception as exc:
                telegram_errors += 1
                print(
                    f"Telegram delivery error for {title!r}: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue

            record = {
                "title": title,
                "summary": event["summary"],
                "url": clean_url(link),
                "image_url": image_url or "",
                "source": event["source"],
                "source_name": event["source_name"],
                "source_id": event["source_id"],
                "adapter_type": event["adapter_type"],
                "resolution_status": event["resolution_status"],
                "resolution_attempts": 1 if (
                    event["adapter_type"] == "google_news_rss"
                    and event["resolution_status"] not in {
                        "resolution_budget_deferred", "legacy_resolution_budget_deferred"
                    }
                    and (
                        event["resolution_status"].startswith("unresolved_google_news_link")
                        or event["resolution_status"].startswith("resolution_error")
                    )
                ) else 0,
                "last_resolution_attempt_at": (
                    now_iso()
                    if event["adapter_type"] == "google_news_rss"
                    and event["resolution_status"] not in {
                        "resolution_budget_deferred", "legacy_resolution_budget_deferred"
                    }
                    else None
                ),
                "categories": categories,
                "primary_category": primary,
                "first_seen_utc": now_iso(),
                "last_seen_utc": now_iso(),
                "deadline": deadline,
                "registration_status": reg_status,
                "registration_status_evidence": reg_evidence,
                "link_status": link_status,
                "link_status_evidence": link_evidence,
                "discovered_url": clean_url(event["discovered_link"]),
                "canonical_url": clean_url(event["canonical_link"]) if event["canonical_link"] else None,
                "source_feed_url": event["source"],
                "application_deadline_raw": None if deadline == UNKNOWN_DEADLINE_TEXT else deadline,
                "deadline_status": "unknown" if deadline == UNKNOWN_DEADLINE_TEXT else "unverified",
                "opportunity_status": "unknown",
                "verification": {
                    "status": "unverified",
                    "official_url": None,
                    "evidence_urls": [],
                    "last_checked_at": None,
                    "deadline_checked_at": None,
                    "eligibility_checked_at": None,
                    "funding_checked_at": None,
                    "notes": ["Discovered via a feed; the resolved publisher page is not yet verified as the organizer's official page."],
                },
            }
            verification_result = event.get("verification_result")
            if isinstance(verification_result, dict):
                record = apply_page_verification(record, verification_result)
            normalized_record = normalize_event_record(event["uid"], record)
            remember_source_alias(
                normalized_record, event["source_id"], event["source_name"],
                event["source"], event["discovered_link"], event["canonical_link"],
            )
            seen[event["uid"]] = normalized_record
            discovered += 1

    state["updated_utc"] = now_iso()
    write_state(state)
    print(
        f"Done. New matching events sent: {discovered}; queued this run: {len(pending_events)}; "
        f"feed errors: {feed_errors}; Telegram errors: {telegram_errors}; total tracked: {len(seen)}"
    )
    if feed_errors == len(sources) or telegram_errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

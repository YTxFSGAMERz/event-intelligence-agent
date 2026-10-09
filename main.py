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

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.txt"
STATE_FILE = ROOT / "data" / "seen_events.json"
USER_AGENT = "EventIntelligenceAgent/0.1 (personal event research; respectful feed polling)"
TIMEOUT = 10
MAX_ALERTS_PER_SOURCE_PER_RUN = 1
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
    raw_url = _http_url(item.get("url"))
    parsed_host = (urlsplit(raw_url).hostname or "").lower() if raw_url else ""
    is_discovery_redirect = parsed_host in {"news.google.com", "www.google.com"}
    canonical_url = _http_url(item.get("canonical_url"))
    if canonical_url is None and raw_url and not is_discovery_redirect:
        canonical_url = clean_url(raw_url)

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
            "official_url": canonical_url,
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
        "discovered_url": raw_url,
        "source_feed_url": _http_url(item.get("source")),
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

    # Discovery pass: gather new entries before sending so the Telegram output can
    # be grouped and sorted by category instead of following feed-source order.
    for source in sources:
        alerts_attempted = 0
        print(f"Checking feed: {source}")
        try:
            feed_response = requests.get(source, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            feed_response.raise_for_status()
            feed = feedparser.parse(feed_response.content)
            if getattr(feed, "bozo", False) and not feed.entries:
                raise RuntimeError(str(getattr(feed, "bozo_exception", "invalid feed")))

            for entry in feed.entries:
                title = str(entry.get("title", "Untitled event")).strip()
                link = str(entry.get("link", "")).strip()
                raw_summary = str(entry.get("summary", entry.get("description", "")))
                summary = unescape(re.sub(r"<[^>]+>", " ", raw_summary))
                content = f"{title}\n{summary}"
                categories = matching_categories(content)
                if not categories:
                    continue

                uid = event_id(link, title)
                if uid in seen:
                    seen[uid]["last_seen_utc"] = now_iso()
                    continue

                if alerts_attempted >= MAX_ALERTS_PER_SOURCE_PER_RUN:
                    print(
                        f"Per-source alert limit reached for {source}; "
                        "remaining new items will be checked on a future run."
                    )
                    break

                primary = primary_category(categories, title)
                pending_events.append({
                    "uid": uid,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "raw_summary": raw_summary,
                    "entry": dict(entry),
                    "categories": categories,
                    "primary_category": primary,
                    "source": source,
                })
                alerts_attempted += 1

        except Exception as exc:
            feed_errors += 1
            print(f"Feed error ({source}): {type(exc).__name__}: {exc}", file=sys.stderr)

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
                "categories": categories,
                "primary_category": primary,
                "first_seen_utc": now_iso(),
                "last_seen_utc": now_iso(),
                "deadline": deadline,
                "registration_status": reg_status,
                "registration_status_evidence": reg_evidence,
                "link_status": link_status,
                "link_status_evidence": link_evidence,
                "discovered_url": clean_url(link),
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
                    "notes": ["Discovered via a feed; official details have not yet been verified."],
                },
            }
            seen[event["uid"]] = normalize_event_record(event["uid"], record)
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

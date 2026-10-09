from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.txt"
STATE_FILE = ROOT / "data" / "seen_events.json"
USER_AGENT = "EventIntelligenceAgent/0.1 (personal event research; respectful feed polling)"
TIMEOUT = 10
MAX_ALERTS_PER_SOURCE_PER_RUN = 1

KEYWORDS = {
    "hackathon": ["hackathon", "hack day", "coding challenge", "buildathon"],
    "tech_fest": ["tech fest", "technology festival", "student festival", "conference", "summit"],
    "prizes": ["cash prize", "prize pool", "prizes", "award", "winner"],
    "swag_gadgets": ["swag", "merchandise", "giveaway", "gadget", "laptop", "phone", "hardware"],
    "funded_travel": ["travel grant", "travel support", "travel stipend", "flight", "airfare",
                      "accommodation covered", "fully funded", "funded travel", "scholarship"],
    "student_open_source": ["open source", "student", "fellowship", "scholarship", "student program"],
    "free_registration": ["free registration", "free ticket", "no registration fee", "free entry",
                          "early bird", "early-bird", "registration opens"],
}
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

def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("seen"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"seen": {}}

def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def matching_categories(text: str) -> list[str]:
    low = text.lower()
    return [category for category, terms in KEYWORDS.items() if any(term in low for term in terms)]

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

def telegram_send(text: str) -> None:
    if os.getenv("DRY_RUN", "").lower() == "true":
        print("\n--- DRY RUN TELEGRAM MESSAGE ---\n" + text + "\n--- END ---")
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram secrets not configured; alert printed to logs instead.")
        print(text)
        return
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(endpoint, json={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }, timeout=TIMEOUT)
    except requests.RequestException as exc:
        # Never include the request URL in the exception: it contains the bot token.
        raise RuntimeError(f"Telegram request failed ({type(exc).__name__})") from None

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok or not payload.get("ok"):
        error_code = payload.get("error_code", response.status_code)
        description = payload.get("description", "Telegram API did not confirm message delivery")
        raise RuntimeError(f"Telegram API error {error_code}: {description}")

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
    discovered = 0
    feed_errors = 0
    telegram_errors = 0

    if not sources:
        print("No sources configured. Add public RSS/Atom URLs to sources.txt.")
        print("No network discovery was performed.")
        return 0

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
                if alerts_attempted >= MAX_ALERTS_PER_SOURCE_PER_RUN:
                    print(
                        f"Per-source alert limit reached for {source}; remaining new items will be checked on a future run."
                    )
                    break
                title = str(entry.get("title", "Untitled event")).strip()
                link = str(entry.get("link", "")).strip()
                summary = str(entry.get("summary", entry.get("description", "")))
                # Feed HTML is untrusted; remove markup and decode basic entities.
                from html import unescape
                summary = unescape(re.sub(r"<[^>]+>", " ", summary))
                content = f"{title}\n{summary}"
                categories = matching_categories(content)
                if not categories:
                    continue
                uid = event_id(link, title)
                if uid in seen:
                    # Keep last-seen timestamp without re-alerting on every run.
                    seen[uid]["last_seen_utc"] = now_iso()
                    continue

                alerts_attempted += 1
                deadline = extract_deadline(content)
                reg_status, reg_evidence = registration_status(content)
                link_status, link_evidence = check_link(link)
                item = {"title": title, "link": link, "summary": summary}
                alert = build_alert(item, categories, deadline, reg_status, reg_evidence,
                                    link_status, link_evidence)
                try:
                    telegram_send(alert)
                except Exception as exc:
                    telegram_errors += 1
                    print(
                        f"Telegram delivery error for {title!r}: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    # Do not mark failed notifications as seen; a later run can retry them.
                    continue

                seen[uid] = {
                    "title": title,
                    "url": clean_url(link),
                    "source": source,
                    "first_seen_utc": now_iso(),
                    "last_seen_utc": now_iso(),
                    "deadline": deadline,
                    "registration_status": reg_status,
                    "link_status": link_status,
                }
                discovered += 1
        except Exception as exc:
            feed_errors += 1
            print(f"Feed error ({source}): {type(exc).__name__}: {exc}", file=sys.stderr)

    state["updated_utc"] = now_iso()
    write_state(state)
    print(
        f"Done. New matching events: {discovered}; feed errors: {feed_errors}; "
        f"Telegram delivery errors: {telegram_errors}; total tracked: {len(seen)}"
    )
    # Fail visibly when every feed fails or any Telegram alert cannot be delivered.
    if feed_errors == len(sources) or telegram_errors:
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

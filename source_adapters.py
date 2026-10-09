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
from urllib.parse import parse_qs, urlsplit, urlunsplit

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
    minimum_interval_seconds: int = 900
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


def _resolve_source_type(url: str) -> tuple[str, str]:
    host = _host(url)
    parts = urlsplit(url)
    if host in {"hackalendar.com", "www.hackalendar.com"} and parts.path.rstrip("/") == "/feed.xml":
        return "hackalendar_rss", "Hackalendar · Upcoming Hackathons"
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
            except Exception:
                pass

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


class GenericRSSAdapter(RSSSourceAdapter):
    """Default adapter for other permitted RSS/Atom feeds."""


ADAPTER_REGISTRY: dict[str, type[RSSSourceAdapter]] = {
    "google_news_rss": GoogleNewsRSSAdapter,
    "hackalendar_rss": HackalendarRSSAdapter,
    "official_blog_rss": OfficialBlogRSSAdapter,
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

    Legacy backfill gets its own small allowance so old records cannot consume
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

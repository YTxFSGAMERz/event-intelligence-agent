import unittest

from source_adapters import (
    FeedResult,
    build_adapters,
    build_source_config,
    normalize_http_url,
    source_health_failure,
    source_health_success,
)
import source_adapters


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
    def test_registry_classifies_and_deduplicates_sources(self):
        google = "https://news.google.com/rss/search?q=hackathon"
        blog = "https://github.blog/changelog/feed/"
        adapters = build_adapters([google, blog, google])

        self.assertEqual(len(adapters), 2)
        self.assertEqual(adapters[0].config.adapter_type, "google_news_rss")
        self.assertEqual(adapters[1].config.adapter_type, "official_blog_rss")
        self.assertNotEqual(adapters[0].config.source_id, adapters[1].config.source_id)

    def test_normalize_url_removes_tracking_parameters(self):
        result = normalize_http_url("HTTPS://Example.org/event/?utm_source=test&ref=feed&id=12#details")
        self.assertEqual(result, "https://example.org/event?id=12")

    def test_google_news_decoder_resolves_modern_encoded_urls(self):
        adapter = source_adapters.RSSSourceAdapter(
            build_source_config("https://news.google.com/rss/search?q=hackathon")
        )
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

    def test_google_news_redirect_resolves_publisher_url(self):
        adapter = build_source_config("https://news.google.com/rss/search?q=hackathon")
        from source_adapters import RSSSourceAdapter

        response = FakeResponse("https://publisher.example/events/abc/?utm_campaign=rss")
        session = FakeSession(response)
        resolved, status = RSSSourceAdapter(adapter).resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=session,
        )
        self.assertEqual(resolved, "https://publisher.example/events/abc")
        self.assertEqual(status, "redirect_resolved")
        self.assertTrue(response.closed)

    def test_google_news_canonical_tag_is_used_if_redirect_stays_on_google(self):
        from source_adapters import RSSSourceAdapter

        html = (
            b'<html><head><link rel="canonical" '
            b'href="https://publisher.example/story?id=7&amp;utm_source=rss"></head><body>'
        )
        response = FakeResponse("https://news.google.com/rss/articles/encoded", html)
        session = FakeSession(response)
        adapter = RSSSourceAdapter(build_source_config("https://news.google.com/rss/search?q=test"))
        resolved, status = adapter.resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=session,
        )
        self.assertEqual(resolved, "https://publisher.example/story?id=7")
        self.assertEqual(status, "canonical_tag_resolved")

    def test_unresolved_google_url_remains_unknown(self):
        from source_adapters import RSSSourceAdapter

        response = FakeResponse("https://news.google.com/rss/articles/encoded", b"<html><head></head>")
        adapter = RSSSourceAdapter(build_source_config("https://news.google.com/rss/search?q=test"))
        resolved, status = adapter.resolve_item_url(
            "https://news.google.com/rss/articles/encoded",
            session=FakeSession(response),
        )
        self.assertIsNone(resolved)
        self.assertEqual(status, "unresolved_google_news_link")

    def test_source_health_failure_keeps_last_success(self):
        config = build_source_config("https://example.org/feed.xml")
        previous = {"last_success_at": "2026-10-08T10:00:00+00:00", "failure_streak": 2}
        failed = source_health_failure(previous, config, ValueError("bad feed"), 120)
        self.assertEqual(failed["last_success_at"], "2026-10-08T10:00:00+00:00")
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


if __name__ == "__main__":
    unittest.main()

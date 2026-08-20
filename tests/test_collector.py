from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from radar.collector import _extract_loose_rss, canonicalize_url, collect_rss, collect_x, is_ai_related, title_fingerprint
from radar.sources import SOURCE_BY_KEY, X_AI_QUERY, X_PRIORITY_HANDLES, X_PRIORITY_QUERIES, Source
from radar.store import Store


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
  <item>
    <title>New open-source LLM agent framework</title>
    <link>https://example.com/post?utm_source=test</link>
    <guid>post-1</guid>
    <description><![CDATA[<p>A useful new project.</p>]]></description>
    <pubDate>Wed, 15 Jul 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


class CollectorTests(unittest.TestCase):
    def test_url_cleanup(self) -> None:
        self.assertEqual(
            canonicalize_url("https://EXAMPLE.com/post/?utm_source=x&id=3#top"),
            "https://example.com/post?id=3",
        )

    def test_fingerprint_ignores_punctuation_and_case(self) -> None:
        self.assertEqual(title_fingerprint("Hello, AI!"), title_fingerprint("hello ai"))

    def test_keyword_filter_supports_chinese(self) -> None:
        self.assertTrue(is_ai_related("一个新的大模型项目"))
        self.assertFalse(is_ai_related("今天的天气预报"))

    @patch("radar.collector._request_bytes", return_value=RSS)
    def test_rss_parser(self, _request: object) -> None:
        source = Source("test", "Test", "global", "news", "https://example.com/feed", "rss")
        items = collect_rss(source)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["external_id"], "post-1")
        self.assertEqual(items[0]["summary"], "A useful new project.")
        self.assertEqual(items[0]["content_type"], "project")

    def test_loose_rss_parser_handles_broken_markup(self) -> None:
        broken = b"""<rss><channel><item><title>AI & tools</title>
        <link>https://example.com/ai</link><description><![CDATA[<p>Useful</p>]]></description>
        </item></channel></rss>"""
        entries = _extract_loose_rss(broken)
        self.assertEqual(entries[0]["title"], "AI & tools")
        self.assertEqual(entries[0]["link"], "https://example.com/ai")

    @patch.dict("os.environ", {"X_BEARER_TOKEN": "test-token"}, clear=True)
    @patch("radar.collector._request_json")
    def test_x_collector_maps_posts_and_obeys_interval(self, request_json: object) -> None:
        request_json.return_value = {
            "data": [{
                "id": "123456789",
                "text": "一个新的开源模型已经发布",
                "author_id": "42",
                "created_at": datetime.now(UTC).isoformat(),
                "lang": "zh",
                "public_metrics": {
                    "like_count": 10,
                    "reply_count": 2,
                    "retweet_count": 3,
                    "quote_count": 1,
                    "bookmark_count": 4,
                },
            }],
            "meta": {"newest_id": "123456789"},
        }
        source = Source(
            "x-test",
            "X Test",
            "global",
            "news",
            "https://api.x.com/2/tweets/search/recent",
            "x",
            query='"AI agent" -is:retweet',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            batch = collect_x(source, store)
            self.assertEqual(len(batch.items), 1)
            self.assertEqual(batch.cursor, "123456789")
            self.assertEqual(batch.resource_count, 1)
            self.assertEqual(batch.items[0]["region"], "china")
            self.assertEqual(batch.items[0]["engagement"], 34)
            requested_url = request_json.call_args.args[0]
            self.assertIn("max_results=30", requested_url)
            self.assertNotIn("expansions", requested_url)
            self.assertEqual(request_json.call_args.args[1]["Authorization"], "Bearer test-token")

            skipped = collect_x(source, store)
            self.assertEqual(skipped.skipped, "daily request budget reached")
            self.assertEqual(request_json.call_count, 1)

    @patch.dict("os.environ", {"X_BEARER_TOKEN": "test-token"}, clear=True)
    @patch("radar.collector._request_json")
    def test_x_ai_prioritizes_accounts_and_uses_keyword_fallback(self, request_json: object) -> None:
        def payload(start: int, count: int, next_token: str = "") -> dict[str, object]:
            posts = [{
                "id": str(start + index),
                "text": f"AI update {start + index} from a priority account",
                "author_id": str(start),
                "created_at": datetime.now(UTC).isoformat(),
                "lang": "en",
                "public_metrics": {},
            } for index in range(count)]
            meta = {"newest_id": str(start + count - 1)}
            if next_token:
                meta["next_token"] = next_token
            return {
                "data": posts,
                "meta": meta,
            }

        request_json.side_effect = [
            payload(100, 10),
            payload(200, 10),
            payload(300, 10),
        ]
        self.assertEqual(len(X_PRIORITY_HANDLES), 30)
        self.assertTrue(all(len(query) <= 512 for query in X_PRIORITY_QUERIES))
        self.assertIn('"DeepSeek"', X_AI_QUERY)
        self.assertIn('"开源模型"', X_AI_QUERY)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            batch = collect_x(SOURCE_BY_KEY["x-ai"], store)

        self.assertEqual(request_json.call_count, 3)
        self.assertEqual(batch.resource_count, 30)
        self.assertEqual(len(batch.items), 30)
        self.assertEqual(batch.cursor, "309")
        self.assertEqual([item["metadata"]["query_group"] for item in batch.items[:20]], [
            "priority",
        ] * 20)
        self.assertTrue(all(item["metadata"]["query_group"] == "discovery" for item in batch.items[20:]))
        self.assertTrue(all("重点账号" in item["tags"] for item in batch.items[:20]))
        self.assertTrue(all("重点账号" not in item["tags"] for item in batch.items[20:]))
        requested_urls = [call.args[0] for call in request_json.call_args_list]
        self.assertIn("max_results=10", requested_urls[0])
        self.assertIn("max_results=10", requested_urls[1])
        self.assertIn("max_results=10", requested_urls[2])
        self.assertIn("DeepSeek", requested_urls[2])
        self.assertTrue(all("since_id" not in url and "start_time" not in url for url in requested_urls))


class StoreTests(unittest.TestCase):
    def test_store_ranks_and_deduplicates_same_title(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            published = datetime.now(UTC).isoformat()
            base = {
                "title": "A new AI tool",
                "url": "https://example.com/a",
                "canonical_url": "https://example.com/a",
                "summary": "Useful tool",
                "author": "A",
                "published_at": published,
                "collected_at": published,
                "region": "global",
                "content_type": "project",
                "raw_score": 2,
                "engagement": 10,
                "tags": ["ai"],
                "metadata": {},
                "fingerprint": title_fingerprint("A new AI tool"),
            }
            store.upsert_items([
                {**base, "source_key": "github-llm", "external_id": "1"},
                {**base, "source_key": "hacker-news", "external_id": "2", "url": "https://example.org/thread"},
            ])
            items = store.query_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source_count"], 2)
            self.assertGreater(items[0]["score"], 50)

    def test_project_uses_first_discovery_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            discovered = datetime.now(UTC).isoformat()
            published = (datetime.now(UTC) - timedelta(days=5)).isoformat()
            store.upsert_items([{
                "source_key": "github-llm",
                "external_id": "old-but-new-to-radar",
                "title": "Newly discovered project",
                "url": "https://example.com/project",
                "canonical_url": "https://example.com/project",
                "summary": "",
                "author": "A",
                "published_at": published,
                "collected_at": discovered,
                "region": "global",
                "content_type": "project",
                "raw_score": 2,
                "engagement": 5,
                "tags": [],
                "metadata": {},
                "fingerprint": title_fingerprint("Newly discovered project"),
            }])
            items = store.query_items(hours=24, content_type="project")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["effective_at"], discovered)

    def test_x_refetch_uses_latest_collection_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            old_time = (datetime.now(UTC) - timedelta(days=5)).isoformat()
            new_time = datetime.now(UTC).isoformat()
            base = {
                "source_key": "x-ai",
                "external_id": "x-post",
                "title": "A useful AI post",
                "url": "https://x.com/i/web/status/x-post",
                "canonical_url": "https://x.com/i/web/status/x-post",
                "summary": "",
                "author": "X 重点账号",
                "published_at": old_time,
                "collected_at": old_time,
                "region": "global",
                "content_type": "news",
                "raw_score": 3,
                "engagement": 10,
                "tags": ["X", "重点账号"],
                "metadata": {},
                "fingerprint": title_fingerprint("A useful AI post"),
            }
            store.upsert_items([base])
            store.upsert_items([{**base, "collected_at": new_time}])
            items = store.query_items(hours=24)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["effective_at"], new_time)


if __name__ == "__main__":
    unittest.main()

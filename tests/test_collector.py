from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from radar import collector as collector_module
from radar.collector import _extract_loose_rss, canonicalize_url, collect_hf_papers, collect_reddit, collect_rss, collect_x, is_ai_related, title_fingerprint
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

REDDIT_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Machine learning discussion</title>
    <link href="https://www.reddit.com/r/MachineLearning/comments/abc123/example/"/>
    <id>t3_abc123</id>
    <updated>2026-09-02T05:00:00+00:00</updated>
    <content>First discussion</content>
  </entry>
  <entry>
    <title>Local model discussion</title>
    <link href="https://www.reddit.com/r/LocalLLaMA/comments/def456/example/"/>
    <id>t3_def456</id>
    <updated>2026-09-02T05:01:00+00:00</updated>
    <content>Second discussion</content>
  </entry>
</feed>"""


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

    @patch("radar.collector._request_bytes", return_value=REDDIT_RSS)
    def test_reddit_sources_share_feed_and_split_results(self, request_bytes: object) -> None:
        collector_module._REDDIT_FEED_CACHE["fetched_at"] = 0.0
        collector_module._REDDIT_FEED_CACHE["items"] = []

        ml_items = collect_reddit(SOURCE_BY_KEY["reddit-ml"])
        llama_items = collect_reddit(SOURCE_BY_KEY["reddit-localllama"])

        self.assertEqual(request_bytes.call_count, 1)
        self.assertEqual([item["external_id"] for item in ml_items], ["t3_abc123"])
        self.assertEqual([item["external_id"] for item in llama_items], ["t3_def456"])
        self.assertEqual(ml_items[0]["source_key"], "reddit-ml")
        self.assertEqual(llama_items[0]["source_key"], "reddit-localllama")
        self.assertEqual(llama_items[0]["metadata"]["via"], "rss")

    @patch("radar.collector._request_json")
    def test_hf_papers_keeps_model_research_and_uses_community_heat(self, request_json: object) -> None:
        request_json.return_value = [{
            "numComments": 3,
            "paper": {
                "id": "2609.00001",
                "title": "Rethinking On-Policy Distillation of Large Language Models",
                "summary": "We improve LLM reasoning through on-policy distillation.",
                "publishedAt": "2026-09-02T00:00:00Z",
                "submittedOnDailyAt": "2026-09-05T00:00:00Z",
                "upvotes": 120,
                "githubStars": 40,
                "ai_keywords": ["large language model", "distillation", "reasoning"],
            },
        }, {
            "numComments": 1,
            "paper": {
                "id": "2609.00002",
                "title": "A Common Measure of Communication for Speech Interfaces",
                "summary": "A measure of communication efficiency for clinical interfaces.",
                "publishedAt": "2026-09-02T00:00:00Z",
                "submittedOnDailyAt": "2026-09-05T00:00:00Z",
                "upvotes": 500,
                "ai_keywords": ["mutual information", "speech interface"],
            },
        }]

        items = collect_hf_papers(SOURCE_BY_KEY["hf-papers"])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["external_id"], "2609.00001")
        self.assertEqual(items[0]["published_at"], "2026-09-05T00:00:00+00:00")
        self.assertEqual(items[0]["engagement"], 556)
        self.assertIn("模型强相关", items[0]["tags"])
        self.assertEqual(items[0]["metadata"]["upvotes"], 120)

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
            self.assertEqual(batch.items[0]["engagement"], 39)
            requested_url = request_json.call_args.args[0]
            self.assertIn("max_results=50", requested_url)
            self.assertNotIn("expansions", requested_url)
            self.assertIn("article%2Cnote_tweet", requested_url)
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
        self.assertIn("max_results=15", requested_urls[0])
        self.assertIn("max_results=15", requested_urls[1])
        self.assertIn("max_results=20", requested_urls[2])
        self.assertIn("DeepSeek", requested_urls[2])
        self.assertIn("has%3Alinks", requested_urls[2])
        self.assertTrue(all("since_id" not in url and "start_time" not in url for url in requested_urls))

    @patch.dict("os.environ", {"X_BEARER_TOKEN": "test-token"}, clear=True)
    @patch("radar.collector._request_json")
    def test_x_article_becomes_hot_discussion_with_real_metrics(self, request_json: object) -> None:
        request_json.return_value = {
            "data": [{
                "id": "2095991462416490862",
                "text": "https://x.com/i/article/2095989703967125509",
                "author_id": "1556653309",
                "created_at": datetime.now(UTC).isoformat(),
                "lang": "en",
                "article": {
                    "title": "Rethinking skills and prompts for GPT-6 Astra",
                    "preview_text": "Coding-agent best practices are changing fast.",
                    "content": {"blocks": [{"text": "A detailed and timely point of view."}]},
                },
                "public_metrics": {
                    "impression_count": 1_562_633,
                    "like_count": 3_614,
                    "retweet_count": 373,
                    "quote_count": 89,
                    "reply_count": 81,
                    "bookmark_count": 8_946,
                },
            }],
            "meta": {"newest_id": "2095991462416490862"},
        }
        source = Source(
            "x-test",
            "X Test",
            "global",
            "news",
            "https://api.x.com/2/tweets/search/recent",
            "x",
            query='"GPT-6" -is:retweet',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            item = collect_x(source, store).items[0]

        self.assertEqual(item["title"], "Rethinking skills and prompts for GPT-6 Astra")
        self.assertEqual(item["content_type"], "discussion")
        self.assertEqual(item["author"], "X")
        self.assertIn("观点", item["tags"])
        self.assertIn("长文", item["tags"])
        self.assertIn("高热度", item["tags"])
        self.assertEqual(item["metadata"]["metrics"]["impression_count"], 1_562_633)
        self.assertGreater(item["metadata"]["discussion_score"], 100)


class StoreTests(unittest.TestCase):
    def test_paper_query_excludes_unrelated_research_and_exposes_community_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            timestamp = datetime.now(UTC).isoformat()

            def paper(
                external_id: str,
                title: str,
                summary: str,
                metadata: dict[str, int],
                source_key: str = "hf-papers",
            ) -> dict[str, object]:
                return {
                    "source_key": source_key,
                    "external_id": external_id,
                    "title": title,
                    "url": f"https://huggingface.co/papers/{external_id}",
                    "canonical_url": f"https://huggingface.co/papers/{external_id}",
                    "summary": summary,
                    "author": "",
                    "published_at": timestamp,
                    "collected_at": timestamp,
                    "region": "global",
                    "content_type": "paper",
                    "raw_score": 10,
                    "engagement": 500,
                    "tags": [],
                    "metadata": metadata,
                    "fingerprint": title_fingerprint(title),
                }

            store.upsert_items([
                paper(
                    "model-paper",
                    "Efficient Reasoning for Large Language Models",
                    "A new inference method for LLM reasoning.",
                    {"upvotes": 88, "comments": 7, "github_stars": 120},
                ),
                paper(
                    "unrelated-paper",
                    "A Longitudinal Study of Urban Commuting",
                    "We study how commuters select rail and bus routes.",
                    {"upvotes": 900, "comments": 40, "github_stars": 0},
                ),
                paper(
                    "social-post",
                    "A Viral Post About Large Language Models",
                    "A social post that mentions an LLM paper but is not the paper itself.",
                    {},
                    source_key="x-ai",
                ),
            ])

            items = store.query_items(hours=24, content_type="paper")

            self.assertEqual([item["title"] for item in items], ["Efficient Reasoning for Large Language Models"])
            self.assertTrue(items[0]["model_relevant"])
            self.assertEqual(items[0]["paper_metrics"], {"upvotes": 88, "comments": 7, "github_stars": 120})

    def test_default_ranking_prioritizes_x_and_applies_source_quotas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            now = datetime.now(UTC)

            def item(source_key: str, external_id: str, *, content_type: str, age_hours: int, engagement: int, raw_score: float, tags: list[str] | None = None) -> dict[str, object]:
                timestamp = (now - timedelta(hours=age_hours)).isoformat()
                title = f"{source_key} item {external_id}"
                return {
                    "source_key": source_key,
                    "external_id": external_id,
                    "title": title,
                    "url": f"https://example.com/{source_key}/{external_id}",
                    "canonical_url": f"https://example.com/{source_key}/{external_id}",
                    "summary": "",
                    "author": "",
                    "published_at": timestamp,
                    "collected_at": timestamp,
                    "region": "global",
                    "content_type": content_type,
                    "raw_score": raw_score,
                    "engagement": engagement,
                    "tags": tags or [],
                    "metadata": {},
                    "fingerprint": title_fingerprint(title),
                }

            records = [
                item("hf-models", f"hf-{index}", content_type="model", age_hours=0, engagement=100_000, raw_score=12)
                for index in range(35)
            ]
            for source_key in (
                "openai-news", "techcrunch-ai", "github-llm", "arxiv-ai", "hf-spaces",
                "devto-ai", "qbitai", "reddit-ml", "simon-willison", "solidot",
            ):
                records.append(item(source_key, source_key, content_type="news", age_hours=6, engagement=1, raw_score=0))
            records.append(item("x-ai", "x-priority", content_type="news", age_hours=20, engagement=1, raw_score=0, tags=["X", "重点账号"]))
            store.upsert_items(records)

            items = store.query_items(hours=24, limit=50)
            primary_sources = [entry["sources"][0]["key"] for entry in items]

            self.assertEqual(primary_sources[0], "x-ai")
            self.assertEqual(primary_sources.count("hf-models"), 30)
            self.assertEqual(len(items), 41)
            self.assertEqual(len(set(primary_sources)), 12)

    def test_x_discussions_rank_by_reach_before_watchlist_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            timestamp = datetime.now(UTC).isoformat()

            def x_item(external_id: str, score: float, tags: list[str]) -> dict[str, object]:
                title = f"X opinion {external_id}"
                return {
                    "source_key": "x-ai",
                    "external_id": external_id,
                    "title": title,
                    "url": f"https://x.com/i/web/status/{external_id}",
                    "canonical_url": f"https://x.com/i/web/status/{external_id}",
                    "summary": "A substantial and timely point of view about AI agents.",
                    "author": "@author",
                    "published_at": timestamp,
                    "collected_at": timestamp,
                    "region": "global",
                    "content_type": "discussion",
                    "raw_score": 8,
                    "engagement": 1_000,
                    "tags": tags,
                    "metadata": {"discussion_score": score, "metrics": {"impression_count": 100_000}},
                    "fingerprint": title_fingerprint(title),
                }

            store.upsert_items([
                x_item("watched", 40, ["X", "重点账号", "观点"]),
                x_item("viral", 120, ["X", "观点", "高热度"]),
            ])
            items = store.query_items(hours=24, content_type="discussion")

            self.assertEqual([item["external_id"] if "external_id" in item else item["url"].rsplit("/", 1)[-1] for item in items], ["viral", "watched"])

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

    def test_x_refetch_keeps_original_publish_time_for_timeliness(self) -> None:
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
            self.assertEqual(store.query_items(hours=24), [])
            items = store.query_items(hours=168)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["effective_at"], old_time)


if __name__ == "__main__":
    unittest.main()

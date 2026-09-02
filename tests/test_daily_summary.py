from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from radar.collector import title_fingerprint
from radar.daily_summary import DailySummaryError, generate_daily_summary, get_or_create_daily_summary
from radar.store import Store


def summary_items() -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "source_key": "x-ai" if index == 1 else "github-llm",
            "title": f"AI update {index}",
            "summary": f"Summary for AI update {index}",
            "content_type": "news" if index == 1 else "project",
            "tags": ["重点账号"] if index == 1 else ["AI"],
            "score": 80 - index,
            "engagement": 100 * index,
            "effective_at": datetime.now(UTC).isoformat(),
            "sources": [{"name": "X AI 热帖" if index == 1 else "GitHub LLM 新项目"}],
        }
        for index in range(1, 4)
    ]


def stored_item(index: int) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    source_key = "x-ai" if index == 1 else "github-llm"
    title = f"AI cached update {index}"
    return {
        "source_key": source_key,
        "external_id": f"daily-{index}",
        "title": title,
        "url": f"https://example.com/{index}",
        "canonical_url": f"https://example.com/{index}",
        "summary": f"Summary {index}",
        "author": "",
        "published_at": now,
        "collected_at": now,
        "region": "global",
        "content_type": "news",
        "raw_score": 2,
        "engagement": 10 * index,
        "tags": ["重点账号"] if index == 1 else ["AI"],
        "metadata": {},
        "fingerprint": title_fingerprint(title),
    }


SUMMARY_PAYLOAD = {
    "headline": "AI 智能体与开源模型升温",
    "overview": "今天的内容主要围绕智能体工具、开源模型和开发工作流展开，讨论从模型能力转向实际应用。",
    "themes": [
        {"name": "智能体", "summary": "工具链继续走向可执行工作流。", "item_ids": [1, 999]},
        {"name": "开源模型", "summary": "新模型强调效率和本地部署。", "item_ids": [2]},
        {"name": "开发工具", "summary": "AI 编程产品继续完善协作能力。", "item_ids": [3]},
    ],
    "key_signals": ["关注智能体从演示走向生产环境"],
    "content_ideas": ["对比今天出现的三类智能体工作流"],
}


class DailySummaryTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(DailySummaryError, "DASHSCOPE_API_KEY"):
            generate_daily_summary(summary_items())

    @patch.dict(
        os.environ,
        {"DASHSCOPE_API_KEY": "test-key", "SUMMARY_MODEL": "deepseek-v4-flash-0731"},
        clear=True,
    )
    @patch("radar.daily_summary.urllib.request.urlopen")
    def test_generates_structured_summary_and_filters_unknown_ids(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": f"```json\n{json.dumps(SUMMARY_PAYLOAD, ensure_ascii=False)}\n```"}}]}
        ).encode("utf-8")
        urlopen.return_value.__enter__.return_value = response

        result, model = generate_daily_summary(summary_items())

        self.assertEqual(model, "deepseek-v4-flash-0731")
        self.assertEqual(result["headline"], SUMMARY_PAYLOAD["headline"])
        self.assertEqual(result["themes"][0]["item_ids"], [1])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertFalse(body["enable_thinking"])
        self.assertIn("representative_items", body["messages"][1]["content"])

    def test_reuses_cached_summary_for_same_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "test.db")
            store.upsert_items(stored_item(index) for index in range(1, 4))
            with patch(
                "radar.daily_summary.generate_daily_summary",
                return_value=(SUMMARY_PAYLOAD, "deepseek-test"),
            ) as generate:
                first = get_or_create_daily_summary(store)
                second = get_or_create_daily_summary(store)

            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(second["headline"], SUMMARY_PAYLOAD["headline"])
            self.assertEqual(generate.call_count, 1)
            cached = store.get_daily_summary(first["summary_date"])
            self.assertIsNotNone(cached)
            self.assertEqual(cached["item_count"], 3)


if __name__ == "__main__":
    unittest.main()

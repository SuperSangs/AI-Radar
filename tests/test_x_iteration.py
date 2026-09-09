import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from radar.collector import collect_x
from radar.sources import SOURCE_BY_KEY
from radar.store import Store
from radar.x_policy import admissible, duplicate
from radar.daily_summary import _prompt_payload, _content_hash


class XIterationTests(unittest.TestCase):
    def test_quality_rejects_viral_promotion_and_keeps_new_official_release(self):
        self.assertFalse(admissible('AI positioned above every model ' * 10, {'like_count': 9999}, article=True))
        self.assertFalse(admissible('AI agents will change everything. ' * 20, {'impression_count': 999999}, article=True))
        self.assertTrue(admissible('Now available: a new API with improved reasoning.', {}, official=True))
        self.assertFalse(admissible('Get 5M free tokens for GPT models. Start the Telegram bot to claim tokens. ' * 4, {'like_count': 500}))
        self.assertFalse(admissible('AI API routing: why pay full price? Our service is below official list. ' * 4, {'like_count': 500}))

    def test_duplicate_keeps_distinct_arguments(self):
        original = 'Our AI agents need reliable execution, robust feedback and reproducible evaluation. ' * 3
        self.assertTrue(duplicate(original + ' https://t.co/a', original.upper() + ' 🚀 https://t.co/b'))
        self.assertFalse(duplicate(original, 'I disagree. LLM reasoning is primarily constrained by training data and evaluation design. ' * 3))

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_seventy_budget_official_first_and_old_posts_rejected(self, request):
        sizes = []
        def response(url, *args, **kwargs):
            params = parse_qs(urlsplit(url).query)
            n = int(params['max_results'][0])
            sizes.append(n)
            self.assertIn('start_time', params)
            if len(sizes) == 1:
                self.assertIn('from:OpenAI', params['query'][0])
                self.assertEqual(params['sort_order'], ['recency'])
            return {'data': [{
                'id': str(len(sizes) * 100 + i),
                'text': 'AI model evaluation now includes reproducible reasoning tasks and detailed latency measurements. ' * 2,
                'created_at': (datetime.now(UTC) - timedelta(hours=30 if i == 0 else 1)).isoformat(),
                'public_metrics': {'like_count': 100},
            } for i in range(n)]}
        request.side_effect = response
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            batch = collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertEqual(sizes, [15, 12, 13, 30])
            self.assertEqual(batch.resource_count, 70)
            self.assertEqual(len(batch.items), 66)
            self.assertTrue(batch.accounted)
            self.assertFalse(store.reserve_x_resources(1, 70))
            self.assertTrue(all(i['metadata']['metrics_fetched_at'] for i in batch.items))

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json', side_effect=TimeoutError)
    def test_unknown_request_outcome_stays_charged(self, request):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            with self.assertRaises(TimeoutError):
                collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertFalse(store.reserve_x_resources(56, 70))
            self.assertTrue(store.reserve_x_resources(55, 70))

    def test_summary_includes_all_seventy_and_invalidates_on_body_change(self):
        items = [{'id': i, 'source_key': 'x-ai', 'title': f'Topic {i}', 'summary': f'Body {i}'} for i in range(70)]
        payload = _prompt_payload(items)
        self.assertEqual(len(payload['representative_items']), 70)
        original = _content_hash(items)
        items[35]['summary'] = 'Corrected content'
        self.assertNotEqual(original, _content_hash(items))
        self.assertEqual(_content_hash(items), _content_hash(list(reversed(items))))

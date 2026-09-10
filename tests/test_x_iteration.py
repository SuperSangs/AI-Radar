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
            self.assertEqual(sizes, [10, 10, 10, 10, 10])
            self.assertEqual(batch.resource_count, 50)
            self.assertEqual(len(batch.items), 45)
            self.assertTrue(batch.accounted)
            self.assertEqual(store.x_resources_remaining(70), 20)
            self.assertEqual(len(store.x_search_progress()), 5)
            self.assertTrue(all(i['metadata']['metrics_fetched_at'] for i in batch.items))

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json', side_effect=TimeoutError)
    def test_unknown_request_outcome_stays_charged(self, request):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            with self.assertRaises(TimeoutError):
                collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertFalse(store.reserve_x_resources(61, 70))
            self.assertTrue(store.reserve_x_resources(60, 70))

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_partial_success_survives_later_failure(self, request):
        request.side_effect = [{'data': [{
            'id': '123', 'text': 'Now available: improved AI reasoning API.',
            'created_at': datetime.now(UTC).isoformat(),
            'public_metrics': {'like_count': 100}}]}, TimeoutError()]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            with self.assertRaises(TimeoutError):
                collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertEqual(store.visible_x_count(), 1)
            self.assertEqual(store.x_resources_remaining(70), 59)
            self.assertEqual(len(store.x_search_progress()), 1)

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_resumes_fixed_window_and_does_not_rescan_completed_windows(self, request):
        calls = []
        def respond(url, *args, **kwargs):
            params = parse_qs(urlsplit(url).query)
            calls.append(params)
            return {'data': [], 'meta': {'next_token': 'page-two'} if len(calls) == 1 else {}}
        request.side_effect = respond
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            collect_x(SOURCE_BY_KEY['x-ai'], store)
            # The official second page preserves start/end and consumes token.
            self.assertEqual(calls[-1]['next_token'], ['page-two'])
            self.assertEqual(calls[0]['start_time'], calls[-1]['start_time'])
            self.assertEqual(calls[0]['end_time'], calls[-1]['end_time'])
            self.assertEqual(store.x_resources_remaining(70), 70)
            first_end = calls[0]['end_time'][0]
            with store.connect() as conn:
                conn.execute("UPDATE source_cursors SET last_request_at=NULL WHERE source_key='x-ai'")
            future = datetime.now(UTC) + timedelta(hours=2)
            with patch('radar.collector.utc_now', return_value=future):
                collect_x(SOURCE_BY_KEY['x-ai'], store)
            official = [c for c in calls[6:] if 'from:OpenAI ' in c['query'][0]]
            self.assertEqual(official[0]['start_time'], [first_end])

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_daily_budget_survives_restarts_and_limits_next_cycle(self, request):
        request.return_value = {'data': [{'id': str(i), 'text': 'irrelevant'} for i in range(10)]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            store = Store(path)
            store.reserve_x_resources(50, 70)
            collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertEqual(request.call_count, 2)
            restarted = Store(path)
            self.assertEqual(restarted.x_resources_remaining(70), 0)
            self.assertTrue(collect_x(SOURCE_BY_KEY['x-ai'], restarted).skipped)
            self.assertEqual(request.call_count, 2)

    def test_summary_includes_all_seventy_and_invalidates_on_body_change(self):
        items = [{'id': i, 'source_key': 'x-ai', 'title': f'Topic {i}', 'summary': f'Body {i}'} for i in range(70)]
        payload = _prompt_payload(items)
        self.assertEqual(len(payload['representative_items']), 70)
        original = _content_hash(items)
        items[35]['summary'] = 'Corrected content'
        self.assertNotEqual(original, _content_hash(items))
        self.assertEqual(_content_hash(items), _content_hash(list(reversed(items))))

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_stops_on_visible_target_not_candidate_count(self, request):
        request.return_value = {'data': [{'id': str(i), 'text': 'irrelevant'} for i in range(10)]}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            with patch.object(store, 'visible_x_count', side_effect=[20, 25, 30]):
                batch = collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(batch.resource_count, 20)
            self.assertEqual(store.x_resources_remaining(70), 50)

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_force_does_not_bypass_paid_budget(self, request):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            store.reserve_x_resources(68, 70)
            self.assertTrue(collect_x(SOURCE_BY_KEY['x-ai'], store, force=True).skipped)
            request.assert_not_called()

    @patch.dict('os.environ', {'X_BEARER_TOKEN': 'test'}, clear=True)
    @patch('radar.collector._request_json')
    def test_empty_paginated_responses_cannot_loop_forever(self, request):
        request.return_value = {'data': [], 'meta': {'next_token': 'still-empty'}}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'test.db')
            collect_x(SOURCE_BY_KEY['x-ai'], store)
            self.assertEqual(request.call_count, 12)
            self.assertEqual(store.x_resources_remaining(70), 70)

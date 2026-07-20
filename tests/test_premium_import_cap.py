import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import requests

import main


class _Response:
    status_code = 200

    def __init__(self, result):
        self._result = result

    def json(self):
        return {'result': self._result}


class _AtomicRedis:
    """Small EVAL contract fake; the lock models Redis script atomicity."""

    def __init__(self):
        self.counts = {}
        self.seen = set()
        self._lock = threading.Lock()

    def post(self, _url, *, json, **_kwargs):
        self.assert_eval_command(json)
        counter_key, seen_key = json[3], json[4]
        limit = int(json[5])
        with self._lock:
            if seen_key in self.seen:
                return _Response(1)
            count = self.counts.get(counter_key, 0)
            if count >= limit:
                return _Response(0)
            self.seen.add(seen_key)
            self.counts[counter_key] = count + 1
            return _Response(1)

    @staticmethod
    def assert_eval_command(command):
        if command[:3] != ['EVAL', main._PREMIUM_IMPORT_CAP_SCRIPT, '2']:
            raise AssertionError(f'unexpected Redis command: {command[:3]}')


class PremiumImportCapTest(unittest.TestCase):
    def setUp(self):
        self.redis = _AtomicRedis()
        self.url = patch.object(main, '_UPSTASH_URL', 'https://redis.test')
        self.token = patch.object(main, '_UPSTASH_TOKEN', 'test-token')
        self.day = patch.object(main.time, 'strftime', return_value='20260720')
        self.post = patch.object(main.requests, 'post', side_effect=self.redis.post)
        self.url.start()
        self.token.start()
        self.day.start()
        self.post.start()

    def tearDown(self):
        patch.stopall()

    def test_concurrent_retries_of_same_document_consume_one_slot(self):
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(
                lambda _index: main._premium_import_cap_ok(
                    'premium-user', 'v38\nthe same document'),
                range(20),
            ))

        self.assertTrue(all(results))
        self.assertEqual(
            self.redis.counts['importcap|v2|premium-user|20260720'],
            1,
        )
        self.assertEqual(len(self.redis.seen), 1)

    def test_five_distinct_documents_are_allowed_and_sixth_is_rejected(self):
        for index in range(main._PREMIUM_DAILY_IMPORT_LIMIT):
            self.assertTrue(main._premium_import_cap_ok(
                'premium-user', f'document-{index}'))

        self.assertFalse(main._premium_import_cap_ok(
            'premium-user', 'document-over-limit'))
        # A retry of an already-counted document remains allowed after the
        # account reaches its distinct-document limit.
        self.assertTrue(main._premium_import_cap_ok(
            'premium-user', 'document-0'))
        self.assertEqual(
            self.redis.counts['importcap|v2|premium-user|20260720'],
            main._PREMIUM_DAILY_IMPORT_LIMIT,
        )

    def test_accounts_and_document_contents_are_isolated(self):
        self.assertTrue(main._premium_import_cap_ok('user-a', 'document'))
        self.assertTrue(main._premium_import_cap_ok('user-b', 'document'))
        self.assertTrue(main._premium_import_cap_ok('user-a', 'other-document'))

        self.assertEqual(self.redis.counts['importcap|v2|user-a|20260720'], 2)
        self.assertEqual(self.redis.counts['importcap|v2|user-b|20260720'], 1)

    def test_redis_transport_failure_preserves_existing_fail_open_policy(self):
        self.post.stop()
        with patch.object(
                main.requests,
                'post',
                side_effect=requests.RequestException('offline')):
            self.assertTrue(main._premium_import_cap_ok('user', 'document'))

    def test_parse_passes_stable_marker_and_markdown_identity(self):
        self.post.stop()
        client = main.app.test_client()
        cap = Mock(return_value=False)
        with patch.object(main, '_authenticate', return_value='premium-user'), \
                patch.object(main, '_rate_limit_ok', return_value=True), \
                patch.object(main.firestore_client, 'is_premium', return_value=True), \
                patch.object(main, '_premium_import_cap_ok', cap):
            response = client.post(
                '/api/parse',
                json={'markdown': '00000: source\n', 'section_type': 'discover'},
                headers={'X-Exam-Trainer-Answer-Markers': 'v38'},
            )

        self.assertEqual(response.status_code, 429)
        cap.assert_called_once_with('premium-user', 'v38\n00000: source\n')


if __name__ == '__main__':
    unittest.main()

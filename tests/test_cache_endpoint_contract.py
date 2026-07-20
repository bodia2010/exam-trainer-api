import json
import unittest
from unittest.mock import patch

import main


class CacheEndpointContractTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    def test_unauthenticated_requests_are_rejected(self):
        with patch.object(main, '_authenticate', return_value=None):
            get = self.client.get('/api/cache?hash=v1|doc|abc')
            post = self.client.post(
                '/api/cache', json={'hash': 'v1|doc|abc', 'value': {}})
        self.assertEqual(get.status_code, 401)
        self.assertEqual(post.status_code, 401)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_requires_hash(self, _authenticate):
        response = self.client.get('/api/cache')
        self.assertEqual(response.status_code, 400)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_requires_hash_and_value(self, _authenticate):
        missing_hash = self.client.post('/api/cache', json={'value': {}})
        missing_value = self.client.post('/api/cache', json={'hash': 'k'})
        self.assertEqual(missing_hash.status_code, 400)
        self.assertEqual(missing_value.status_code, 400)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_reports_hit_and_miss(self, _authenticate):
        with patch.object(main, '_cache_get', return_value=None):
            miss = self.client.get('/api/cache?hash=v1|doc|abc')
        self.assertEqual(miss.status_code, 200)
        self.assertEqual(miss.get_json(), {'hit': False})

        with patch.object(
                main, '_cache_get',
                return_value=json.dumps({'lesen_teil1': []})):
            hit = self.client.get('/api/cache?hash=v1|doc|abc')
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.get_json(), {'hit': True, 'value': {'lesen_teil1': []}})

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_writes_a_genuinely_new_key(self, _authenticate):
        with patch.object(main, '_cache_get', return_value=None) as cache_get, \
                patch.object(main, '_cache_set') as cache_set:
            response = self.client.post(
                '/api/cache',
                json={'hash': 'v1|group|new', 'value': [{'a': 1}]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        cache_get.assert_called_once_with('v1|group|new')
        cache_set.assert_called_once_with(
            'v1|group|new', json.dumps([{'a': 1}]))

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_is_a_noop_when_the_same_value_already_exists(
            self, _authenticate):
        existing = json.dumps([{'a': 1}])
        with patch.object(main, '_cache_get', return_value=existing), \
                patch.object(main, '_cache_set') as cache_set:
            response = self.client.post(
                '/api/cache',
                json={'hash': 'v1|group|same', 'value': [{'a': 1}]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        cache_set.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_never_overwrites_an_existing_different_value(
            self, _authenticate):
        # This is the core regression: an authenticated account (any tier)
        # must not be able to silently replace an already-cached entry —
        # in particular the hand-curated, PDF-verified `doc` cache the
        # whole app converges on — with different content just by POSTing
        # the same hash.
        existing = json.dumps({'beschwerde': [{'answer': 'a'}]})
        with patch.object(main, '_cache_get', return_value=existing), \
                patch.object(main, '_cache_set') as cache_set:
            response = self.client.post(
                '/api/cache',
                json={
                    'hash': 'v30.v38|doc|curated-key',
                    'value': {'beschwerde': [{'answer': 'wrong'}]},
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})
        cache_set.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_rejects_oversized_values(self, _authenticate):
        huge_value = {'padding': 'x' * (main._CACHE_MAX_VALUE_BYTES + 1)}
        with patch.object(main, '_cache_get') as cache_get, \
                patch.object(main, '_cache_set') as cache_set:
            response = self.client.post(
                '/api/cache',
                json={'hash': 'v1|doc|huge', 'value': huge_value},
            )
        self.assertEqual(response.status_code, 413)
        cache_get.assert_not_called()
        cache_set.assert_not_called()


if __name__ == '__main__':
    unittest.main()

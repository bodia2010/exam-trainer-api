import hashlib
import json
import unittest
from unittest.mock import patch

import main


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


_DOC_KEY = f'v30.v38|doc|{_hash("flagship-course")}'
_GROUP_KEY = f'v38|group|{_hash("some-group")}'
_LEGACY_KEY = _hash('legacy-bare-hash')


class CacheEndpointContractTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    def test_unauthenticated_requests_are_rejected(self):
        with patch.object(main, '_authenticate', return_value=None):
            get = self.client.get(f'/api/cache?hash={_DOC_KEY}')
            post = self.client.post(
                '/api/cache', json={'hash': _DOC_KEY, 'value': {}})
        self.assertEqual(get.status_code, 401)
        self.assertEqual(post.status_code, 401)

    # --- key validation (GET and POST) -----------------------------------

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_requires_hash(self, _authenticate):
        response = self.client.get('/api/cache')
        self.assertEqual(response.status_code, 400)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_accepts_legacy_bare_hash_format(self, _authenticate):
        with patch.object(main, '_cache_get', return_value=None) as cache_get:
            response = self.client.get(f'/api/cache?hash={_LEGACY_KEY}')
        self.assertEqual(response.status_code, 200)
        cache_get.assert_called_once_with(_LEGACY_KEY)

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_rejects_path_and_query_like_keys(self, _authenticate):
        for bad_key in (
            'v30.v38|doc|../../../etc/passwd',
            'v30.v38|doc|' + 'a' * 64 + '?x=1',
            'not-a-hash-at-all',
            'v1|unknown-type|' + 'a' * 64,
            'a' * 63,  # one short of a real sha256 hex digest
            'a' * 65,  # one long
            'A' * 64,  # uppercase not accepted — hashes are always lowercase hex
        ):
            with patch.object(main, '_cache_get') as cache_get:
                response = self.client.get(f'/api/cache?hash={bad_key}')
            self.assertEqual(
                response.status_code, 400, msg=f'accepted bad key: {bad_key!r}')
            cache_get.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_rejects_overlong_key(self, _authenticate):
        with patch.object(main, '_cache_get') as cache_get:
            response = self.client.get(
                '/api/cache?hash=' + 'v1|doc|' + 'a' * 500)
        self.assertEqual(response.status_code, 400)
        cache_get.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_rejects_invalid_key_format(self, _authenticate):
        with patch.object(main, '_cache_probe') as cache_probe:
            response = self.client.post(
                '/api/cache',
                json={'hash': '../secrets', 'value': {'a': 1}},
            )
        self.assertEqual(response.status_code, 400)
        cache_probe.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_requires_hash_and_value(self, _authenticate):
        missing_hash = self.client.post('/api/cache', json={'value': {}})
        missing_value = self.client.post(
            '/api/cache', json={'hash': _DOC_KEY})
        self.assertEqual(missing_hash.status_code, 400)
        self.assertEqual(missing_value.status_code, 400)

    # --- malformed JSON bodies ---------------------------------------------

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_rejects_non_object_json_bodies(self, _authenticate):
        for bad_body in ([], 'a string', 42, None, True):
            response = self.client.post(
                '/api/cache',
                data=json.dumps(bad_body),
                content_type='application/json',
            )
            self.assertEqual(
                response.status_code, 400,
                msg=f'non-object body {bad_body!r} did not get a clean 400')
            # Must never leak a raw 500/traceback for a well-formed-but-
            # wrong-shaped JSON body.
            self.assertNotIn(b'Traceback', response.data)

    # --- reads ---------------------------------------------------------

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_reports_hit_and_miss(self, _authenticate):
        with patch.object(main, '_cache_get', return_value=None):
            miss = self.client.get(f'/api/cache?hash={_DOC_KEY}')
        self.assertEqual(miss.status_code, 200)
        self.assertEqual(miss.get_json(), {'hit': False})

        with patch.object(
                main, '_cache_get',
                return_value=json.dumps({'lesen_teil1': []})):
            hit = self.client.get(f'/api/cache?hash={_DOC_KEY}')
        self.assertEqual(hit.status_code, 200)
        self.assertEqual(hit.get_json(), {'hit': True, 'value': {'lesen_teil1': []}})

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_get_degrades_a_corrupt_stored_value_to_a_miss(
            self, _authenticate):
        with patch.object(main, '_cache_get', return_value='{not json'):
            response = self.client.get(f'/api/cache?hash={_DOC_KEY}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'hit': False})

    # --- writes: the core trust-model regression --------------------------

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_never_creates_a_genuinely_new_key(self, _authenticate):
        # This is the core regression for CR-17: no authenticated caller —
        # free or premium — may be the first writer of a not-yet-cached
        # shared key. `_cache_probe` returning None (confirmed absent) must
        # refuse, not create.
        with patch.object(
                main, '_cache_probe', return_value=None) as cache_probe:
            response = self.client.post(
                '/api/cache',
                json={'hash': _GROUP_KEY, 'value': [{'a': 1}]},
            )
        self.assertEqual(response.status_code, 403)
        cache_probe.assert_called_once_with(_GROUP_KEY)
        # No write function of any kind may exist/be reachable from this
        # branch — assert main module no longer exposes a public setter
        # cache_endpoint could have called.
        self.assertFalse(hasattr(main, '_cache_set'))

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_is_a_noop_when_the_same_value_already_exists(
            self, _authenticate):
        existing = json.dumps([{'a': 1}])
        with patch.object(main, '_cache_probe', return_value=existing):
            response = self.client.post(
                '/api/cache',
                json={'hash': _GROUP_KEY, 'value': [{'a': 1}]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'ok': True})

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_rejects_an_existing_different_value_as_conflict(
            self, _authenticate):
        # The curated `doc` entry the whole app converges on must be
        # provably un-overwritable: a differing value against an existing
        # key is a rejected, observable conflict — never a silent 200.
        existing = json.dumps({'beschwerde': [{'answer': 'a'}]})
        with patch.object(main, '_cache_probe', return_value=existing):
            response = self.client.post(
                '/api/cache',
                json={
                    'hash': _DOC_KEY,
                    'value': {'beschwerde': [{'answer': 'wrong'}]},
                },
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json(), {'ok': False, 'conflict': True})

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_rejects_oversized_values(self, _authenticate):
        huge_value = {'padding': 'x' * (main._CACHE_MAX_VALUE_BYTES + 1)}
        with patch.object(main, '_cache_probe') as cache_probe:
            response = self.client.post(
                '/api/cache',
                json={'hash': _DOC_KEY, 'value': huge_value},
            )
        self.assertEqual(response.status_code, 413)
        cache_probe.assert_not_called()

    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_post_never_returns_success_when_upstash_is_unavailable(
            self, _authenticate):
        with patch.object(
                main, '_cache_probe',
                side_effect=main._CacheUnavailable('boom')):
            response = self.client.post(
                '/api/cache',
                json={'hash': _DOC_KEY, 'value': {'a': 1}},
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('ok', response.get_json())


if __name__ == '__main__':
    unittest.main()

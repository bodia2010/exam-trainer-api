"""Regression tests for the /api/cache write-once race (commit b9345b2).

b9345b2 fixed cache poisoning by adding a "write-once" check to the POST
handler, implemented as two separate calls: read the existing value, then
write if absent. That is a classic TOCTOU race -- two concurrent requests
to the same not-yet-cached key can both observe "absent" and both proceed
to write, with the second one silently winning (last-writer-wins), which
defeats the entire point of "never overwrite existing content."

The endpoint has since been redesigned so that POST /api/cache can NEVER
create a new key at all. It only ever:
  - returns 200 idempotently if an existing cached value byte-for-byte
    matches what was posted;
  - returns 409 if an existing value differs;
  - returns 403 if the key does not exist yet (creation is refused, not
    raced).
The only Redis operation the handler performs is a read (`_cache_probe`);
it issues no SET/write of any kind. New keys can only be created
out-of-band by tools/inject_curated.py, which talks to Upstash directly
with separate credentials.

This module proves two things:
  1. The concurrency harness used here (barrier-synchronized threads plus
     an injected delay between "read" and "decide") is actually capable
     of reproducing the old race -- see
     CacheConcurrencyRegressionTest.test_old_buggy_get_then_set_pattern_races_proving_harness_is_sound,
     which runs a local reimplementation of the OLD two-step pattern
     through the exact same harness and asserts the race DOES happen.
  2. The real /api/cache POST route, run through that same harness against
     a mocked, artificially-delayed `_cache_probe`, never creates anything
     and never issues a write, no matter how the concurrent requests
     interleave.
"""

import hashlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import main


def _valid_hash(label: str) -> str:
    """A real 64-hex-char sha256 digest, so it satisfies main._valid_cache_key
    regardless of which distinct label each test uses to keep keys apart."""
    return hashlib.sha256(label.encode('utf-8')).hexdigest()


def _key(label: str, key_type: str = 'group') -> str:
    return f'v1|{key_type}|{_valid_hash(label)}'


def _post_json(hash_key, value):
    """Each concurrent request gets its own test client, per the project's
    guidance that a Flask test client should not be assumed safe to fire
    truly concurrently from multiple native threads."""
    client = main.app.test_client()
    return client.post('/api/cache', json={'hash': hash_key, 'value': value})


class CacheConcurrencyRegressionTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    # ------------------------------------------------------------------
    # 1. Real endpoint: concurrent POSTs to an ABSENT key never create it.
    # ------------------------------------------------------------------
    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-concurrency')
    def test_concurrent_posts_to_absent_key_never_create_it(
            self, _authenticate, _rate_limit_ok):
        """N concurrent POSTs with DIFFERENT values race for the SAME
        not-yet-cached key. Under the old get-then-set design every one of
        them could observe "absent" and proceed to write, with the last
        writer silently winning. The redesigned handler has no write path
        at all, so every concurrent request to an absent key must come
        back refused (403) -- never a 200 "created" response -- and the
        underlying `requests.post` call the old `_cache_set` used to make
        must never fire.

        A `threading.Barrier` sized to the request count forces every
        thread's mocked `_cache_probe` call to rendezvous before any of
        them proceeds, so all N requests are guaranteed to observe
        "absent" at the same instant on every run -- this does not rely on
        scheduler luck to hit the race window."""
        key = _key('absent-race-target')
        n = 8
        barrier = threading.Barrier(n)

        def slow_probe_absent(probed_key):
            barrier.wait(timeout=5)
            time.sleep(0.05)  # widen the historical TOCTOU window
            return None

        with patch.object(
                main, '_cache_probe', side_effect=slow_probe_absent) as probe, \
                patch.object(main.requests, 'post') as http_post:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = [
                    pool.submit(_post_json, key, {'value_index': i})
                    for i in range(n)
                ]
                responses = [f.result(timeout=10) for f in futures]

        self.assertEqual(probe.call_count, n)
        for response in responses:
            self.assertEqual(response.status_code, 403)
            body = response.get_json()
            self.assertNotIn('ok', body)  # never the success/creation shape

        # The write capability isn't just unused here -- it no longer
        # exists in main.py at all.
        self.assertFalse(
            hasattr(main, '_cache_set'),
            'the old write function must be fully removed, not merely '
            'unused, so there is no code path left that could ever '
            'create a key from this endpoint')
        http_post.assert_not_called()

    # ------------------------------------------------------------------
    # 2. Harness sanity check: the OLD get-then-set pattern DOES race
    #    under this exact mechanism, proving test #1 above is meaningful.
    # ------------------------------------------------------------------
    def test_old_buggy_get_then_set_pattern_races_proving_harness_is_sound(self):
        """Deliberately NOT run against the real Flask app. This is a
        minimal, local reimplementation of the OLD two-step pattern from
        commit b9345b2 (read, then write-if-absent, as two separate
        operations with no atomicity between them) against a plain dict
        standing in for Redis, driven through the same
        barrier-plus-delay concurrency mechanism used above.

        If this reproduction did NOT race, it would mean the harness
        itself is too weak to ever have caught the original bug, and the
        "zero writes" / "all refused" assertions elsewhere in this file
        would be trivially true rather than meaningful. This test proves
        the opposite: under the old pattern, every one of N concurrent
        callers observes "absent" and every one of them writes, with
        whichever thread runs last silently winning -- exactly the bug
        the redesign eliminates by removing the write capability
        entirely."""
        n = 6
        store = {}
        write_log = []
        barrier = threading.Barrier(n)

        def _old_buggy_get_then_set(key, value):
            existing = store.get(key)   # OLD _cache_get()
            # Every thread must have already done its "get" before any of
            # them is allowed to act on the result -- this is the TOCTOU
            # window commit b9345b2 left open.
            barrier.wait(timeout=5)
            time.sleep(0.02)
            if existing is None:
                store[key] = value      # OLD _cache_set()
                write_log.append(value)

        threads = [
            threading.Thread(target=_old_buggy_get_then_set, args=(key, i))
            for i, key in enumerate(['k'] * n)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(
            len(write_log), n,
            'every thread should have observed "absent" simultaneously '
            'and every one of them should have written -- that IS the '
            'race. If this ever fails, the harness lost its ability to '
            'reproduce the original bug and the assertions elsewhere in '
            'this file no longer prove anything.')
        # Whoever ran last silently won -- the defining symptom of the bug.
        self.assertIn('k', store)
        self.assertIn(store['k'], write_log)

    # ------------------------------------------------------------------
    # 3. Real endpoint: identical value to an EXISTING key is idempotent,
    #    concurrently, with no write.
    # ------------------------------------------------------------------
    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-concurrency')
    def test_concurrent_identical_value_posts_to_existing_key_are_idempotent_no_write(
            self, _authenticate, _rate_limit_ok):
        key = _key('existing-identical')
        value = {'lesen_teil1': [{'q': 1}]}
        existing_serialized = json.dumps(value)
        n = 2
        barrier = threading.Barrier(n)

        def slow_probe_hit(probed_key):
            barrier.wait(timeout=5)
            time.sleep(0.05)
            return existing_serialized

        with patch.object(
                main, '_cache_probe', side_effect=slow_probe_hit) as probe, \
                patch.object(main.requests, 'post') as http_post:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futures = [pool.submit(_post_json, key, value) for _ in range(n)]
                responses = [f.result(timeout=10) for f in futures]

        for response in responses:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {'ok': True})
        self.assertEqual(probe.call_count, n)
        http_post.assert_not_called()

    # ------------------------------------------------------------------
    # 4. Real endpoint: one matching + one differing concurrent POST
    #    against the SAME existing key -- matching wins, differing is
    #    rejected, stored value never changes.
    # ------------------------------------------------------------------
    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-concurrency')
    def test_concurrent_matching_and_differing_posts_to_same_existing_key(
            self, _authenticate, _rate_limit_ok):
        key = _key('existing-mixed')
        stored_value = {'beschwerde': [{'answer': 'a'}]}
        conflicting_value = {'beschwerde': [{'answer': 'WRONG'}]}
        existing_serialized = json.dumps(stored_value)
        barrier = threading.Barrier(2)

        def slow_probe_hit(probed_key):
            barrier.wait(timeout=5)
            time.sleep(0.05)
            # Always the same already-stored value -- nothing in this
            # endpoint's design is capable of changing it, which is
            # exactly what this test verifies.
            return existing_serialized

        with patch.object(
                main, '_cache_probe', side_effect=slow_probe_hit) as probe, \
                patch.object(main.requests, 'post') as http_post:
            with ThreadPoolExecutor(max_workers=2) as pool:
                matching_future = pool.submit(_post_json, key, stored_value)
                conflicting_future = pool.submit(
                    _post_json, key, conflicting_value)
                matching_response = matching_future.result(timeout=10)
                conflicting_response = conflicting_future.result(timeout=10)

        self.assertEqual(matching_response.status_code, 200)
        self.assertEqual(matching_response.get_json(), {'ok': True})
        self.assertEqual(conflicting_response.status_code, 409)
        self.assertEqual(
            conflicting_response.get_json(), {'ok': False, 'conflict': True})
        self.assertEqual(probe.call_count, 2)
        # The core claim of this test: the stored value never changed, and
        # nothing ever attempted to change it.
        http_post.assert_not_called()


if __name__ == '__main__':
    unittest.main()

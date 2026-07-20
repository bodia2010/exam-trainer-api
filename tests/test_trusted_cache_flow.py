import hashlib
import json
import unittest
from unittest.mock import patch

import line_extraction
import main


def _key(version: str, key_type: str, hash_input: str) -> str:
    digest = hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
    return f'{version}|{key_type}|{digest}'


class TrustedCacheFlowTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()
        self.auth = patch.object(main, '_authenticate', return_value='premium-uid')
        self.rate = patch.object(main, '_rate_limit_ok', return_value=True)
        self.token = patch.object(main, '_UPSTASH_TOKEN', 'test-upstash-token')
        self.auth.start()
        self.rate.start()
        self.token.start()

    def tearDown(self):
        patch.stopall()

    def _parse(self, markdown, section_type, parsed):
        with patch.object(main, '_call_gemini', return_value=json.dumps(parsed)), \
                patch.object(main.firestore_client, 'is_premium', return_value=True), \
                patch.object(main, '_premium_import_cap_ok', return_value=True), \
                patch.object(main, '_global_discover_cap_ok', return_value=True):
            return self.client.post(
                '/api/parse',
                json={'markdown': markdown, 'section_type': section_type},
                headers={'X-Exam-Trainer-Answer-Markers': 'v38'},
            )

    def test_discover_proof_binds_raw_markdown_key_and_restores_free_read(self):
        raw = 'Kapitel A\nKapitel B\n'
        numbered = line_extraction.number_markdown(raw) + '\n'
        parsed = [{
            'section_type': 'beschwerde',
            'variant_number': 1,
            'start_line': 0,
            'anchor': 'Kapitel A',
        }]
        expected_key = _key('v30', 'discover', f'discover|{raw}')

        parse_response = self._parse(numbered, 'discover', parsed)
        self.assertEqual(parse_response.status_code, 200)
        proof = parse_response.headers.get(main._CACHE_PROOF_HEADER)
        self.assertIsNotNone(proof)
        self.assertTrue(main._valid_cache_proof(expected_key, parsed, proof))

        store = {}

        def probe(key):
            return store.get(key)

        def set_if_absent(key, serialized):
            if key in store:
                return False
            store[key] = serialized
            return True

        with patch.object(main, '_cache_probe', side_effect=probe), \
                patch.object(main, '_cache_set_if_absent', side_effect=set_if_absent):
            publish = self.client.post(
                '/api/cache',
                json={'hash': expected_key, 'value': parsed, 'proof': proof},
            )
        self.assertEqual(publish.status_code, 200)
        self.assertEqual(publish.get_json(), {'ok': True, 'created': True})

        # A different authenticated user can now consume the shared result;
        # no /api/parse (and therefore no Gemini call) is involved.
        with patch.object(main, '_authenticate', return_value='free-uid'), \
                patch.object(main, '_cache_get', side_effect=lambda key: store.get(key)), \
                patch.object(main, '_call_gemini') as gemini:
            cached = self.client.get(f'/api/cache?hash={expected_key}')
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.get_json(), {'hit': True, 'value': parsed})
        gemini.assert_not_called()

    def test_group_proof_matches_flutter_key_and_rejects_tampering(self):
        markdown = 'Eine Beschwerde'
        parsed = [{
            'variant_number': 1,
            'texts': [{'title': 'Text', 'content': 'Inhalt'}],
            'questions': [
                {
                    'number': 1,
                    'type': 'choice',
                    'text': 'Frage 1',
                    'answer': 'a',
                    'options': [{'letter': 'a', 'text': 'Antwort'}],
                },
                {
                    'number': 2,
                    'type': 'choice',
                    'text': 'Frage 2',
                    'answer': 'b',
                    'options': [{'letter': 'b', 'text': 'Antwort'}],
                },
            ],
        }]
        expected_key = _key(
            'v38', 'group', f'group|beschwerde|{markdown}')
        response = self._parse(markdown, 'beschwerde', parsed)
        self.assertEqual(response.status_code, 200)
        proof = response.headers.get(main._CACHE_PROOF_HEADER)
        self.assertTrue(main._valid_cache_proof(expected_key, parsed, proof))

        other_key = _key('v38', 'group', 'group|beschwerde|other')
        with patch.object(main, '_cache_probe', return_value=None), \
                patch.object(main, '_cache_set_if_absent') as cache_set:
            wrong_key = self.client.post('/api/cache', json={
                'hash': other_key,
                'value': parsed,
                'proof': proof,
            })
            wrong_value = self.client.post('/api/cache', json={
                'hash': expected_key,
                'value': [{'variant_number': 999}],
                'proof': proof,
            })
        self.assertEqual(wrong_key.status_code, 403)
        self.assertEqual(wrong_value.status_code, 403)
        cache_set.assert_not_called()

    def test_schema_invalid_backend_result_gets_no_publish_capability(self):
        malformed = [{
            'variant_number': 1,
            'texts': [{'title': 'Text', 'content': 'Inhalt'}],
            'questions': [
                {
                    'number': 1,
                    'type': 'choice',
                    'answer': 'z',
                    'options': [{'letter': 'a', 'text': 'Antwort'}],
                },
                {
                    'number': 2,
                    'type': 'choice',
                    'answer': 'b',
                    'options': [{'letter': 'b', 'text': 'Antwort'}],
                },
            ],
        }]
        response = self._parse('Eine Beschwerde', 'beschwerde', malformed)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(main._CACHE_PROOF_HEADER, response.headers)

    def test_legacy_or_malformed_discovery_gets_no_publish_capability(self):
        parsed = []
        with patch.object(main, '_call_gemini', return_value='[]'), \
                patch.object(main.firestore_client, 'is_premium', return_value=True), \
                patch.object(main, '_premium_import_cap_ok', return_value=True), \
                patch.object(main, '_global_discover_cap_ok', return_value=True):
            legacy = self.client.post(
                '/api/parse',
                json={'markdown': '00000: source\n', 'section_type': 'discover'},
            )
        self.assertEqual(legacy.status_code, 200)
        self.assertNotIn(main._CACHE_PROOF_HEADER, legacy.headers)

        malformed = self._parse('raw unnumbered source', 'discover', parsed)
        self.assertEqual(malformed.status_code, 200)
        self.assertNotIn(main._CACHE_PROOF_HEADER, malformed.headers)


if __name__ == '__main__':
    unittest.main()

import os
import sys
import unittest
from unittest.mock import patch

import requests


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'promptfoo'))
import gemini_schema_provider as provider  # noqa: E402


class _Response:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def _gemini_response(usage=None):
    data = {
        'candidates': [
            {'content': {'parts': [{'text': '[{"variant_number": 1}]'}]}},
        ],
    }
    if usage is not None:
        data['usageMetadata'] = usage
    return data


class PromptfooProviderUsageTest(unittest.TestCase):
    def _call(self, model, usage):
        options = {'config': {'model': model}}
        context = {'vars': {'section_type': 'lesen_teil1'}}
        with (
            patch.dict(os.environ, {'GOOGLE_API_KEY': 'test-key'}),
            patch.object(
                provider.requests,
                'post',
                return_value=_Response(_gemini_response(usage)),
            ),
        ):
            return provider.call_api('private fixture text', options, context)

    def test_reports_promptfoo_usage_and_flash_lite_standard_tier_cost(self):
        result = self._call(
            'gemini-3.1-flash-lite',
            {
                'promptTokenCount': 1000,
                'candidatesTokenCount': 200,
                'thoughtsTokenCount': 50,
                'totalTokenCount': 1250,
            },
        )

        self.assertEqual(
            result['tokenUsage'],
            {
                'prompt': 1000,
                'completion': 200,
                'total': 1250,
                'numRequests': 1,
                'completionDetails': {
                    'reasoning': 50,
                    'acceptedPrediction': 0,
                    'rejectedPrediction': 0,
                },
            },
        )
        self.assertAlmostEqual(result['cost'], 0.000625)

    def test_provider_keeps_bounded_http_timeout(self):
        options = {'config': {'model': 'gemini-3.1-flash-lite'}}
        context = {'vars': {'section_type': 'hoeren_teil1'}}
        with (
            patch.dict(os.environ, {'GOOGLE_API_KEY': 'test-key'}),
            patch.object(
                provider.requests,
                'post',
                return_value=_Response(_gemini_response({
                    'promptTokenCount': 1,
                    'candidatesTokenCount': 1,
                    'totalTokenCount': 2,
                })),
            ) as post,
        ):
            provider.call_api('fixture', options, context)

        self.assertEqual(post.call_args.kwargs['timeout'], 100)

    def test_uses_discovery_model_rates(self):
        result = self._call(
            'gemini-3.5-flash',
            {
                'promptTokenCount': 1000,
                'candidatesTokenCount': 200,
                'thoughtsTokenCount': 50,
                'totalTokenCount': 1250,
            },
        )

        self.assertAlmostEqual(result['cost'], 0.00375)

    def test_unknown_ab_model_reports_usage_without_inventing_cost(self):
        result = self._call(
            'gemini-future-experiment',
            {
                'promptTokenCount': 10,
                'candidatesTokenCount': 3,
                'totalTokenCount': 13,
            },
        )

        self.assertEqual(result['tokenUsage']['total'], 13)
        self.assertNotIn('cost', result)

    def test_cached_input_uses_cached_rate_and_is_reported(self):
        result = self._call(
            'gemini-3.5-flash',
            {
                'promptTokenCount': 1000,
                'cachedContentTokenCount': 400,
                'candidatesTokenCount': 200,
                'thoughtsTokenCount': 50,
                'totalTokenCount': 1250,
            },
        )

        self.assertEqual(result['tokenUsage']['cached'], 400)
        self.assertAlmostEqual(result['cost'], 0.00321)

    def test_missing_usage_does_not_report_misleading_zero_cost(self):
        result = self._call('gemini-3.1-flash-lite', None)

        self.assertNotIn('tokenUsage', result)
        self.assertNotIn('cost', result)

    def test_malformed_counts_are_sanitized_and_total_is_derived(self):
        result = self._call(
            'gemini-3.1-flash-lite',
            {
                'promptTokenCount': -10,
                'candidatesTokenCount': 4.9,
                'thoughtsTokenCount': 'secret',
            },
        )

        self.assertEqual(
            result['tokenUsage'],
            {'prompt': 0, 'completion': 4, 'total': 4, 'numRequests': 1},
        )
        self.assertAlmostEqual(result['cost'], 0.000006)

    def test_unexpected_error_does_not_leak_url_key_body_or_prompt(self):
        options = {'config': {'model': 'gemini-3.1-flash-lite'}}
        context = {'vars': {'section_type': 'lesen_teil1'}}
        sensitive = 'https://example.invalid?key=secret private fixture text'
        with (
            patch.dict(os.environ, {'GOOGLE_API_KEY': 'secret'}),
            patch.object(provider.requests, 'post', side_effect=RuntimeError(sensitive)),
        ):
            result = provider.call_api('private fixture text', options, context)

        self.assertEqual(
            result,
            {'output': '', 'error': 'Gemini request failed: RuntimeError'},
        )
        self.assertNotIn('secret', result['error'])
        self.assertNotIn('private fixture text', result['error'])

    def test_http_error_without_response_is_still_redacted(self):
        options = {'config': {'model': 'gemini-3.1-flash-lite'}}
        context = {'vars': {'section_type': 'lesen_teil1'}}
        response = _Response({})
        response.raise_for_status = lambda: (_ for _ in ()).throw(
            requests.HTTPError('https://example.invalid?key=secret'),
        )
        with (
            patch.dict(os.environ, {'GOOGLE_API_KEY': 'secret'}),
            patch.object(provider.requests, 'post', return_value=response),
        ):
            result = provider.call_api('private fixture text', options, context)

        self.assertEqual(
            result,
            {'output': '', 'error': 'Gemini request failed: HTTP unknown'},
        )
        self.assertNotIn('secret', result['error'])


if __name__ == '__main__':
    unittest.main()

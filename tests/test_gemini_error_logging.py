import unittest
from unittest.mock import Mock, patch

import main


class GeminiErrorLoggingTest(unittest.TestCase):
    def test_logs_only_structured_upstream_status(self):
        response = Mock(status_code=400)
        response.json.return_value = {
            'error': {
                'status': 'INVALID_ARGUMENT',
                'message': 'sensitive upstream detail',
            },
        }

        with patch.object(main.requests, 'post', return_value=response), \
                patch.object(main, '_gemini_model', return_value='test-model'), \
                patch.dict(main.os.environ, {'GEMINI_API_KEY': 'secret-key'}), \
                patch('builtins.print') as log:
            with self.assertRaises(main.GeminiError) as raised:
                main._call_gemini(
                    'private PDF content',
                    section_type='discover',
                    is_premium=True,
                )

        self.assertEqual(raised.exception.status_code, 502)
        message = log.call_args.args[0]
        self.assertEqual(
            message,
            'GEMINI_UPSTREAM_ERROR section_type=discover tariff=premium '
            'model=test-model http_status=400 api_status=INVALID_ARGUMENT',
        )
        self.assertNotIn('private PDF content', message)
        self.assertNotIn('secret-key', message)
        self.assertNotIn('sensitive upstream detail', message)

    def test_non_json_error_body_is_logged_as_unknown(self):
        response = Mock(status_code=500)
        response.json.side_effect = ValueError('not JSON')

        with patch.object(main.requests, 'post', return_value=response), \
                patch.object(main, '_gemini_model', return_value='test-model'), \
                patch('builtins.print') as log:
            with self.assertRaises(main.GeminiError):
                main._call_gemini('content', section_type='beschwerde')

        self.assertIn('api_status=unknown', log.call_args.args[0])


if __name__ == '__main__':
    unittest.main()

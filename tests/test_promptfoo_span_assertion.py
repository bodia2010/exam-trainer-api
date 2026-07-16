import importlib.util
import json
from pathlib import Path
import unittest


ASSERTIONS_PATH = (
    Path(__file__).resolve().parents[1] / 'promptfoo' / 'assertions.py'
)
SPEC = importlib.util.spec_from_file_location(
    'exam_trainer_promptfoo_assertions', ASSERTIONS_PATH
)
promptfoo_assertions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promptfoo_assertions)


class SpanTextsResolveCleanlyTest(unittest.TestCase):
    MARKDOWN = '\n'.join([
        'Synthetic heading',
        'First synthetic body line.',
        'Second synthetic body line.',
        'Trailing source line.',
    ])

    def _assertion_result(self, text, section_type='lesen_teil2', markdown=None):
        output = json.dumps([{
            'variant_number': 42,
            'version': None,
            'texts': [text],
        }])
        context = {
            'vars': {
                'section_type': section_type,
                'markdown': markdown if markdown is not None else self.MARKDOWN,
            }
        }
        return promptfoo_assertions.span_texts_resolve_cleanly(output, context)

    def assertRejected(self, text):
        result = self._assertion_result(text)
        self.assertFalse(result['pass'], result['reason'])
        self.assertEqual(result['score'], 0)

    def test_accepts_valid_span_and_heading(self):
        result = self._assertion_result({
            'start_line': 0,
            'end_line': 2,
            'heading_lines': [0],
        })

        self.assertTrue(result['pass'], result['reason'])
        self.assertEqual(result['score'], 1)

    def test_accepts_exact_missing_span_sentinel(self):
        result = self._assertion_result({
            'start_line': -1,
            'end_line': -1,
        })

        self.assertTrue(result['pass'], result['reason'])
        self.assertEqual(result['score'], 1)

    def test_rejects_negative_non_sentinel_spans(self):
        for start, end in ((-1, 0), (0, -1), (-2, -2)):
            with self.subTest(start=start, end=end):
                self.assertRejected({'start_line': start, 'end_line': end})

    def test_rejects_inverted_span(self):
        self.assertRejected({'start_line': 2, 'end_line': 1})

    def test_rejects_out_of_range_spans(self):
        line_count = len(self.MARKDOWN.split('\n'))
        for start, end in ((0, line_count), (line_count, line_count)):
            with self.subTest(start=start, end=end):
                self.assertRejected({'start_line': start, 'end_line': end})

    def test_rejects_boolean_and_string_span_indices(self):
        for start, end in ((True, 1), (0, False), ('0', 1), (0, '1')):
            with self.subTest(start=start, end=end):
                self.assertRejected({'start_line': start, 'end_line': end})

    def test_rejects_non_integer_heading_lines(self):
        for heading in ('0', True):
            with self.subTest(heading=heading):
                self.assertRejected({
                    'start_line': 0,
                    'end_line': 2,
                    'heading_lines': [heading],
                })

    def test_accepts_adjacent_heading_before_span(self):
        result = self._assertion_result({
            'start_line': 1,
            'end_line': 2,
            'heading_lines': [0],
        })

        self.assertTrue(result['pass'], result['reason'])

    def test_accepts_captured_blank_separated_heading_pattern(self):
        result = self._assertion_result(
            {
                'start_line': 2,
                'end_line': 3,
                'heading_lines': [0],
            },
            markdown='Heading\n\nBody\nEnding',
        )

        self.assertTrue(result['pass'], result['reason'])

    def test_rejects_distant_or_nonblank_separated_heading(self):
        cases = (
            ('Heading\n\n\nBody', 3, 3, 0),
            ('Heading\nnot blank\nBody', 2, 2, 0),
        )
        for markdown, start, end, heading in cases:
            with self.subTest(markdown=markdown):
                result = self._assertion_result(
                    {
                        'start_line': start,
                        'end_line': end,
                        'heading_lines': [heading],
                    },
                    markdown=markdown,
                )
                self.assertFalse(result['pass'], result['reason'])

    def test_rejects_heading_after_body_as_next_passage(self):
        result = self._assertion_result(
            {
                'start_line': 0,
                'end_line': 0,
                'heading_lines': [2],
            },
            markdown='Body\n\nNext passage heading',
        )

        self.assertFalse(result['pass'], result['reason'])

    def test_missing_span_sentinel_rejects_heading_lines(self):
        self.assertRejected({
            'start_line': -1,
            'end_line': -1,
            'heading_lines': [0],
        })

    def test_non_span_section_is_no_op(self):
        result = self._assertion_result(
            {'start_line': -9, 'end_line': 'invalid'},
            section_type='beschwerde',
        )

        self.assertTrue(result['pass'], result['reason'])
        self.assertEqual(result['score'], 1)

    def test_non_span_section_with_invalid_json_is_no_op(self):
        result = promptfoo_assertions.span_texts_resolve_cleanly(
            'not valid JSON',
            {'vars': {'section_type': 'beschwerde'}},
        )

        self.assertTrue(result['pass'], result['reason'])
        self.assertEqual(result['score'], 1)


if __name__ == '__main__':
    unittest.main()

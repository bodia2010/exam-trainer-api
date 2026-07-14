import unittest

import span_resolution


class ResolveTelefonnotizSpansTest(unittest.TestCase):
    def test_resolves_valid_spans_with_numeric_strings_bullet_strip_and_slash_index(self):
        parsed = [
            {
                'variant_number': 7,
                'versions': [
                    {
                        'label': None,
                        'answer': {
                            'call_type': 'Angebot',
                            'name': 'Meyer',
                            'telefonnummer': '123',
                            'weitere_informationen': [
                                {
                                    'start_line': '1',
                                    'end_line': '1',
                                    'slash_index': '1',
                                },
                                {'start_line': '2', 'end_line': '3'},
                            ],
                            'zu_erledigen': 'zurueckrufen',
                        },
                    }
                ],
            }
        ]
        markdown = 'Intro\n• links / rechts\n- Termin ver-\nschieben'

        result = span_resolution.resolve_telefonnotiz_spans(parsed, markdown)

        self.assertIs(result, parsed)
        self.assertEqual(
            result[0]['versions'][0]['answer']['weitere_informationen'],
            ['rechts', 'Termin verschieben'],
        )

    def test_exact_missing_sentinel_resolves_to_nicht_angegeben(self):
        parsed = [
            {
                'versions': [
                    {
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': '-1', 'end_line': '-1'}
                            ]
                        }
                    }
                ]
            }
        ]

        result = span_resolution.resolve_telefonnotiz_spans(parsed, 'only line')

        self.assertEqual(
            result[0]['versions'][0]['answer']['weitere_informationen'],
            ['(nicht angegeben)'],
        )

    def test_invalid_negative_inverted_out_of_range_and_bad_shape_become_sentinel(self):
        parsed = [
            {
                'versions': [
                    {
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': -1, 'end_line': 0},
                                {'start_line': 1, 'end_line': 0},
                                {'start_line': 0, 'end_line': 99},
                                {'start_line': 0},
                            ]
                        }
                    }
                ]
            }
        ]

        markdown = 'sensitive-token-one\nsensitive-token-two'

        with self.assertLogs('span_resolution', level='WARNING') as logs:
            result = span_resolution.resolve_telefonnotiz_spans(parsed, markdown)

        self.assertEqual(
            result[0]['versions'][0]['answer']['weitere_informationen'],
            ['(nicht angegeben)'] * 4,
        )
        self.assertEqual(len(logs.output), 4)
        self.assertNotIn('sensitive-token-one', '\n'.join(logs.output))
        self.assertNotIn('sensitive-token-two', '\n'.join(logs.output))

    def test_bool_start_end_and_slash_index_are_rejected(self):
        parsed = [
            {
                'versions': [
                    {
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': True, 'end_line': 0},
                                {'start_line': 0, 'end_line': False},
                                {'start_line': 0, 'end_line': 0, 'slash_index': True},
                            ]
                        }
                    }
                ]
            }
        ]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_telefonnotiz_spans(parsed, 'secret')

        self.assertEqual(
            result[0]['versions'][0]['answer']['weitere_informationen'],
            ['(nicht angegeben)', '(nicht angegeben)', '(nicht angegeben)'],
        )

    def test_non_list_weitere_informationen_is_left_untouched(self):
        parsed = [{'versions': [{'answer': {'weitere_informationen': 'legacy'}}]}]

        result = span_resolution.resolve_telefonnotiz_spans(parsed, 'line')

        self.assertEqual(
            result[0]['versions'][0]['answer']['weitere_informationen'],
            'legacy',
        )


class ResolveUniversalTextSpansTest(unittest.TestCase):
    def test_resolves_valid_spans_to_legacy_title_content_with_heading_strings(self):
        parsed = [
            {
                'variant_number': 1,
                'texts': [
                    {
                        'title': 'Text A',
                        'start_line': '0',
                        'end_line': '5',
                        'heading_lines': ['0'],
                    }
                ],
            }
        ]
        markdown = 'Ueberschrift\n\nSilben-\ntrennung\nSprecher: Hallo\n'

        result = span_resolution.resolve_universal_text_spans(parsed, markdown)

        self.assertEqual(
            result[0]['texts'],
            [
                {
                    'title': 'Text A',
                    'content': '**Ueberschrift**\n\nSilbentrennung\nSprecher: Hallo',
                }
            ],
        )
        self.assertNotIn('start_line', result[0]['texts'][0])
        self.assertNotIn('end_line', result[0]['texts'][0])
        self.assertNotIn('heading_lines', result[0]['texts'][0])

    def test_absent_heading_lines_is_valid(self):
        parsed = [{'texts': [{'title': 'Plain', 'start_line': 0, 'end_line': 1}]}]

        result = span_resolution.resolve_universal_text_spans(parsed, 'one\ntwo')

        self.assertEqual(
            result[0]['texts'],
            [{'title': 'Plain', 'content': 'one\ntwo'}],
        )

    def test_exact_missing_sentinel_keeps_title_and_uses_content_sentinel(self):
        parsed = [
            {'texts': [{'title': 'Missing Passage', 'start_line': '-1', 'end_line': '-1'}]}
        ]

        result = span_resolution.resolve_universal_text_spans(parsed, 'line')

        self.assertEqual(
            result[0]['texts'],
            [{'title': 'Missing Passage', 'content': '(nicht angegeben)'}],
        )

    def test_invalid_negative_inverted_and_out_of_range_spans_use_content_sentinel(self):
        parsed = [
            {
                'texts': [
                    {'title': 'Negative', 'start_line': -1, 'end_line': 0},
                    {'title': 'Inverted', 'start_line': 1, 'end_line': 0},
                    {'title': 'Range', 'start_line': 0, 'end_line': 2},
                ]
            }
        ]

        with self.assertLogs('span_resolution', level='WARNING') as logs:
            result = span_resolution.resolve_universal_text_spans(parsed, 'one\ntwo')

        self.assertEqual(
            result[0]['texts'],
            [
                {'title': 'Negative', 'content': '(nicht angegeben)'},
                {'title': 'Inverted', 'content': '(nicht angegeben)'},
                {'title': 'Range', 'content': '(nicht angegeben)'},
            ],
        )
        self.assertEqual(len(logs.output), 3)

    def test_bool_start_end_are_rejected(self):
        parsed = [
            {
                'texts': [
                    {'title': 'Bool start', 'start_line': True, 'end_line': 0},
                    {'title': 'Bool end', 'start_line': 0, 'end_line': False},
                ]
            }
        ]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(parsed, 'secret')

        self.assertEqual(
            result[0]['texts'],
            [
                {'title': 'Bool start', 'content': '(nicht angegeben)'},
                {'title': 'Bool end', 'content': '(nicht angegeben)'},
            ],
        )

    def test_null_heading_lines_is_valid(self):
        parsed = [
            {'texts': [{'title': 'Plain', 'start_line': 0, 'end_line': 0, 'heading_lines': None}]},
        ]

        result = span_resolution.resolve_universal_text_spans(parsed, 'body')

        self.assertEqual(result[0]['texts'], [{'title': 'Plain', 'content': 'body'}])

    def test_heading_lines_must_be_list_when_non_null(self):
        parsed = [
            {'texts': [{'title': 'Bad headings', 'start_line': 0, 'end_line': 0, 'heading_lines': (0,)}]},
        ]

        with self.assertLogs('span_resolution', level='WARNING') as logs:
            result = span_resolution.resolve_universal_text_spans(parsed, 'raw secret')

        self.assertEqual(result[0]['texts'], [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}])
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('raw secret', '\n'.join(logs.output))
        self.assertNotIn('Bad headings', '\n'.join(logs.output))

    def test_heading_lines_entries_are_coerced_and_must_stay_inside_span(self):
        parsed = [
            {'texts': [{'title': 'Bool heading', 'start_line': 0, 'end_line': 1, 'heading_lines': [True]}]},
            {'texts': [{'title': 'Text heading', 'start_line': 0, 'end_line': 1, 'heading_lines': ['x']}]},
            {'texts': [{'title': 'Outside heading', 'start_line': 1, 'end_line': 1, 'heading_lines': ['0']}]},
        ]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(parsed, 'heading\nbody')

        for item in result:
            self.assertEqual(
                item['texts'],
                [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}],
            )

    def test_non_dict_text_item_becomes_whole_item_sentinel(self):
        parsed = [{'texts': ['not a dict']}]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(parsed, 'line')

        self.assertEqual(
            result[0]['texts'],
            [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}],
        )

    def test_non_list_texts_is_left_untouched(self):
        parsed = [{'texts': 'legacy'}]

        result = span_resolution.resolve_universal_text_spans(parsed, 'line')

        self.assertEqual(result, [{'texts': 'legacy'}])


if __name__ == '__main__':
    unittest.main()

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

    def test_version_metadata_survives_and_invalid_values_are_dropped(self):
        parsed = [
            {
                'versions': [
                    {
                        'metadata': {'voice_gender': 'female'},
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': 0, 'end_line': 0},
                            ],
                        },
                    },
                    {
                        'metadata': {
                            'voice_gender': 'feminine',
                            'speaker_voice_genders': [{'speaker': 'A', 'voice_gender': 'male'}],
                        },
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': '-1', 'end_line': '-1'},
                            ],
                        },
                    },
                    {
                        'metadata': 'bad',
                        'answer': {
                            'weitere_informationen': [
                                {'start_line': '-1', 'end_line': '-1'},
                            ],
                        },
                    },
                ],
            }
        ]

        result = span_resolution.resolve_telefonnotiz_spans(parsed, 'Termin')

        self.assertEqual(result[0]['versions'][0]['metadata'], {'voice_gender': 'female'})
        self.assertEqual(
            result[0]['versions'][1]['metadata'],
            {'speaker_voice_genders': [{'speaker': 'A', 'voice_gender': 'male'}]},
        )
        self.assertNotIn('metadata', result[0]['versions'][2])


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

    def test_text_metadata_survives_span_resolution(self):
        parsed = [
            {
                'texts': [
                    {
                        'title': 'Nummer 36 Andrea',
                        'start_line': 0,
                        'end_line': 1,
                        'metadata': {'voice_gender': 'female'},
                    },
                    {
                        'title': 'Dialog',
                        'start_line': 2,
                        'end_line': 2,
                        'metadata': {
                            'speaker_voice_genders': [
                                {'speaker': ' Herr Becker ', 'voice_gender': 'male'},
                                {'speaker': '', 'voice_gender': 'female'},
                                {'speaker': 'Frau Keller', 'voice_gender': 'feminine'},
                            ],
                        },
                    },
                ],
            }
        ]
        markdown = 'Hallo\nAndrea spricht\nHerr Becker: Guten Tag'

        result = span_resolution.resolve_universal_text_spans(parsed, markdown)

        self.assertEqual(
            result[0]['texts'][0],
            {
                'title': 'Nummer 36 Andrea',
                'content': 'Hallo\nAndrea spricht',
                'metadata': {'voice_gender': 'female'},
            },
        )
        self.assertEqual(
            result[0]['texts'][1],
            {
                'title': 'Dialog',
                'content': 'Herr Becker: Guten Tag',
                'metadata': {
                    'speaker_voice_genders': [
                        {'speaker': 'Herr Becker', 'voice_gender': 'male'},
                    ],
                },
            },
        )

    def test_invalid_text_metadata_degrades_without_breaking_span_resolution(self):
        parsed = [
            {
                'texts': [
                    {
                        'title': 'Invalid metadata',
                        'start_line': 0,
                        'end_line': 0,
                        'metadata': {'voice_gender': 'feminine'},
                    }
                ],
            }
        ]

        result = span_resolution.resolve_universal_text_spans(parsed, 'body')

        self.assertEqual(
            result[0]['texts'],
            [{'title': 'Invalid metadata', 'content': 'body'}],
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
            {'texts': [{'title': 'Bool heading', 'start_line': 0, 'end_line': 2, 'heading_lines': [True]}]},
            {'texts': [{'title': 'Text heading', 'start_line': 0, 'end_line': 2, 'heading_lines': ['x']}]},
            {'texts': [{'title': 'Outside heading', 'start_line': 2, 'end_line': 2, 'heading_lines': ['0']}]},
        ]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(
                parsed,
                'heading\nnon-blank gap\nbody',
            )

        for item in result:
            self.assertEqual(
                item['texts'],
                [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}],
            )

    def test_adjacent_heading_before_body_expands_real_fixture_pattern(self):
        lines = [f'unused {index}' for index in range(75)]
        lines[5] = 'First heading'
        lines[6] = ''
        lines[7] = 'First body'
        lines[18] = 'First ending'
        lines[54] = 'Second heading'
        lines[55] = '   '
        lines[56] = 'Second body'
        lines[74] = 'Second ending'
        parsed = [{
            'texts': [
                {
                    'title': 'First',
                    'start_line': 7,
                    'end_line': 18,
                    'heading_lines': [5],
                },
                {
                    'title': 'Second',
                    'start_line': 56,
                    'end_line': 74,
                    'heading_lines': [54],
                },
            ],
        }]

        result = span_resolution.resolve_universal_text_spans(
            parsed,
            '\n'.join(lines),
        )

        self.assertTrue(result[0]['texts'][0]['content'].startswith(
            '**First heading**\n\nFirst body'
        ))
        self.assertTrue(result[0]['texts'][1]['content'].startswith(
            '**Second heading**\n\nSecond body'
        ))

    def test_adjacent_heading_after_body_is_rejected_as_next_passage(self):
        parsed = [{
            'texts': [{
                'title': 'After',
                'start_line': 0,
                'end_line': 0,
                'heading_lines': [2],
            }],
        }]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(
                parsed,
                'Body\n\nTrailing heading',
            )

        self.assertEqual(
            result[0]['texts'],
            [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}],
        )

    def test_distant_and_nonblank_separated_headings_remain_invalid(self):
        parsed = [
            {'texts': [{
                'title': 'Distant',
                'start_line': 3,
                'end_line': 3,
                'heading_lines': [0],
            }]},
            {'texts': [{
                'title': 'Crosses content',
                'start_line': 2,
                'end_line': 2,
                'heading_lines': [0],
            }]},
        ]

        with self.assertLogs('span_resolution', level='WARNING'):
            result = span_resolution.resolve_universal_text_spans(
                parsed,
                'Heading\nnon-blank gap\nBody\nOther body',
            )

        for item in result:
            self.assertEqual(
                item['texts'],
                [{'title': '(nicht angegeben)', 'content': '(nicht angegeben)'}],
            )

    def test_h4_matching_question_title_corrects_same_speaker_gender(self):
        parsed = [{'texts': [
            {
                'title': 'Nummer 39 Lattermann',
                'start_line': 0,
                'end_line': 1,
                'metadata': {'voice_gender': 'female'},
            },
            {
                'title': 'Nummer 40 Bernhardt',
                'start_line': 3,
                'end_line': 4,
                'metadata': {'voice_gender': 'male'},
            },
        ]}]
        markdown = '\n'.join([
            'Nummer 39 Lattermann',
            'Hallo, Guido Lattermann von der Firma Top.',
            '39. Herr Lattermann',
            'Nummer 40 Bernhardt',
            'Bernhardt, Geschäftsleitung. Guten Tag.',
            '40. Frau Bernhardt',
        ])

        result = span_resolution.resolve_universal_text_spans(
            parsed,
            markdown,
            section_type='hoeren_teil4',
        )

        self.assertEqual(result[0]['texts'][0]['metadata']['voice_gender'], 'male')
        self.assertEqual(result[0]['texts'][1]['metadata']['voice_gender'], 'female')

    def test_h4_absent_titled_third_party_does_not_override_narrator(self):
        parsed = [{'texts': [{
            'title': 'Nummer 37 Frau Plassberg',
            'start_line': 0,
            'end_line': 1,
            'metadata': {'voice_gender': 'male'},
        }]}]
        markdown = '\n'.join([
            'Nummer 37 Frau Plassberg',
            'Hallo, hier ist Zeuner, der neue Assistent der Geschäftsleitung.',
            '37. Frau Plassberg',
        ])

        result = span_resolution.resolve_universal_text_spans(
            parsed,
            markdown,
            section_type='hoeren_teil4',
        )

        self.assertEqual(result[0]['texts'][0]['metadata']['voice_gender'], 'male')

    def test_h4_span_starting_at_transcript_keeps_self_identification(self):
        parsed = [{'texts': [{
            'title': 'Nummer 40 Bernhardt',
            'start_line': 0,
            'end_line': 0,
            'metadata': {'voice_gender': 'male'},
        }]}]
        markdown = '\n'.join([
            'Bernhardt, Geschäftsleitung. Guten Tag.',
            '40. Frau Bernhardt',
        ])

        result = span_resolution.resolve_universal_text_spans(
            parsed,
            markdown,
            section_type='hoeren_teil4',
        )

        self.assertEqual(result[0]['texts'][0]['metadata']['voice_gender'], 'female')

    def test_h1_sole_explicit_header_normalizes_headerless_editions(self):
        parsed = [
            {'variant_number': 2},
            {'variant_number': 3},
            {'variant_number': 99},
        ]
        markdown = (
            'Hören Teil 1 (вариант №2)\n'
            'erste Edition\n<<<ITEM>>>\nheaderlose Fortsetzung'
        )

        result = span_resolution.normalize_h1_variant_numbers(parsed, markdown)

        self.assertEqual([item['variant_number'] for item in result], [2, 2, 2])

    def test_h1_multiple_explicit_headers_are_not_globally_rewritten(self):
        parsed = [{'variant_number': 2}, {'variant_number': 3}]
        markdown = (
            'Hören Teil 1 (вариант №2)\n'
            'Hören Teil 1 (вариант №3)'
        )

        result = span_resolution.normalize_h1_variant_numbers(parsed, markdown)

        self.assertEqual([item['variant_number'] for item in result], [2, 3])

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

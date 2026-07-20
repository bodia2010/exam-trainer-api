import unittest

import cache_validation


class CacheValidationTest(unittest.TestCase):
    def _universal(self, section_type):
        count = cache_validation.UNIVERSAL_QUESTION_COUNTS[section_type]
        return [{
            'variant_number': 1,
            'texts': [{'title': 'Text', 'content': 'Inhalt'}],
            'questions': [
                {
                    'number': index + 1,
                    'type': 'choice',
                    'answer': 'a',
                    'options': [{'letter': 'a', 'text': 'Antwort'}],
                }
                for index in range(count)
            ],
        }]

    def test_all_universal_types_match_flutter_structural_contract(self):
        for section_type in cache_validation.UNIVERSAL_QUESTION_COUNTS:
            with self.subTest(section_type=section_type):
                self.assertTrue(cache_validation.valid_group(
                    self._universal(section_type), section_type))

    def test_all_bespoke_types_match_flutter_structural_contract(self):
        pair = {
            'dialogue': 'Guten Tag',
            'richtig_falsch': {'answer': True},
            'multiple_choice': {
                'correct_letter': 'a',
                'options': [{'letter': 'a', 'text': 'Antwort'}],
            },
        }
        fixtures = {
            'hoeren_teil1': [{
                'variant_number': 1,
                'question_pairs': [pair, pair, pair],
            }],
            'telefonnotiz': [{
                'variant_number': 1,
                'versions': [{
                    'monologue': 'Nachricht',
                    'answer': {
                        'call_type': 'Anruf',
                        'name': 'Name',
                        'telefonnummer': '(nicht angegeben)',
                        'weitere_informationen': ['Information'],
                        'zu_erledigen': 'Antworten',
                    },
                }],
            }],
            'sprachbausteine_teil1': [{
                'variant_number': 1,
                'letter_text': 'Text [46]',
                'all_options': [{'letter': 'a', 'text': 'damit'}],
                'answers': [{
                    'question_number': 46,
                    'letter': 'a',
                    'word': 'damit',
                }],
            }],
        }
        for section_type, value in fixtures.items():
            with self.subTest(section_type=section_type):
                self.assertTrue(cache_validation.valid_group(
                    value, section_type))

    def test_invalid_or_unresolved_group_is_never_cache_eligible(self):
        invalid_answer = self._universal('beschwerde')
        invalid_answer[0]['questions'][0]['answer'] = 'not-an-option'
        sentinel = self._universal('beschwerde')
        sentinel[0]['texts'] = cache_validation.SAME_SENTINEL
        for value in ([], invalid_answer, sentinel, [{'variant_number': 1}]):
            with self.subTest(value=value):
                self.assertFalse(cache_validation.valid_group(
                    value, 'beschwerde'))

    def test_discovery_requires_a_correctable_unique_exercise_boundary(self):
        raw = 'Beschwerde Variante 1\nAufgabentext'
        valid = [{
            'section_type': 'beschwerde',
            'variant_number': 1,
            'start_line': 99,
            'anchor': 'Beschwerde Variante 1',
        }]
        self.assertTrue(cache_validation.valid_discovery(valid, raw))
        self.assertFalse(cache_validation.valid_discovery([], raw))
        self.assertFalse(cache_validation.valid_discovery(
            [{**valid[0], 'anchor': 'hallucinated heading'}], raw))
        self.assertFalse(cache_validation.valid_discovery(
            [valid[0], dict(valid[0])], raw))


if __name__ == '__main__':
    unittest.main()

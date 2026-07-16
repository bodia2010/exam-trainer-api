import importlib.util
import json
import unittest
from pathlib import Path

from prompts import PROMPTS


ASSERTIONS_PATH = (
    Path(__file__).resolve().parents[1] / 'promptfoo' / 'assertions.py'
)
SPEC = importlib.util.spec_from_file_location(
    'exam_trainer_promptfoo_voice_metadata_assertions', ASSERTIONS_PATH
)
assertions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assertions)


def _output(value):
    return json.dumps(value, ensure_ascii=False)


class HoerenTeil4VoiceMetadataAssertionTest(unittest.TestCase):
    context = {
        'vars': {
            'section_type': 'hoeren_teil4',
            'expected_text_voice_genders': '["female", "male", "female"]',
            'markdown': 'unused\nbody',
        }
    }

    def _result(self, genders):
        texts = []
        for gender in genders:
            text = {'title': gender, 'start_line': 1, 'end_line': 1}
            if gender is not None:
                text['metadata'] = {'voice_gender': gender}
            texts.append(text)
        return assertions.hoeren_teil4_voice_metadata_matches_fixture(
            _output([{'texts': texts}]), self.context
        )

    def test_valid_output_passes(self):
        self.assertTrue(self._result(['female', 'male', 'female'])['pass'])

    def test_missing_metadata_fails(self):
        self.assertFalse(self._result(['female', None, 'female'])['pass'])

    def test_wrong_gender_fails(self):
        self.assertFalse(self._result(['unknown', 'male', 'female'])['pass'])

    def test_explicit_matching_question_title_corrects_model_gender(self):
        output = _output([{'texts': [{
            'title': 'Nummer 40 Bernhardt',
            'start_line': 0,
            'end_line': 1,
            'metadata': {'voice_gender': 'male'},
        }]}])
        context = {'vars': {
            'section_type': 'hoeren_teil4',
            'expected_text_voice_genders': '["female"]',
            'markdown': (
                'Nummer 40 Bernhardt\n'
                'Bernhardt, Geschäftsleitung. Guten Tag.\n'
                '40. Frau Bernhardt'
            ),
        }}

        result = assertions.hoeren_teil4_voice_metadata_matches_fixture(
            output,
            context,
        )

        self.assertTrue(result['pass'], result['reason'])


class HoerenTeil1SpeakerMetadataAssertionTest(unittest.TestCase):
    context = {
        'vars': {
            'section_type': 'hoeren_teil1',
            'expected_dialogue_speaker_sets': (
                '[["Frau", "Herr", "TN 1"], ["Chef", "Frau"]]'
            ),
            'expected_items': 2,
            'markdown': 'erste Edition\n<<<ITEM>>>\nzweite Edition',
        }
    }

    @staticmethod
    def _pair(dialogue, hints=None):
        pair = {
            'dialogue': dialogue,
        }
        if hints is not None:
            pair['metadata'] = {'speaker_voice_genders': hints}
        return pair

    def _result(self, objects):
        return assertions.hoeren_teil1_speaker_voice_metadata_exact(
            _output(objects), self.context
        )

    @staticmethod
    def _valid_hints():
        return [
            {'speaker': 'Frau', 'voice_gender': 'female'},
            {'speaker': 'Herr', 'voice_gender': 'male'},
            {'speaker': 'TN 1', 'voice_gender': 'unknown'},
        ]

    @staticmethod
    def _second_hints():
        return [
            {'speaker': 'Chef', 'voice_gender': 'male'},
            {'speaker': 'Frau', 'voice_gender': 'female'},
        ]

    def _all_valid_objects(self):
        return [
            {'question_pairs': [self._pair(
                'Frau : Guten Tag.\nHerr : Hallo.\nTN 1-Guten Morgen.',
                self._valid_hints(),
            )]},
            {'question_pairs': [self._pair(
                'Chef : Wir sprechen.\nFrau : Einverstanden.',
                self._second_hints(),
            )]},
        ]

    def test_valid_output_passes(self):
        self.assertTrue(self._result(self._all_valid_objects())['pass'])

    def test_missing_metadata_fails(self):
        objects = self._all_valid_objects()
        objects[0]['question_pairs'][0].pop('metadata')
        self.assertFalse(self._result(objects)['pass'])

    def test_invented_speaker_fails(self):
        hints = self._valid_hints() + [
            {'speaker': 'Andrea', 'voice_gender': 'female'},
        ]
        objects = self._all_valid_objects()
        objects[0]['question_pairs'][0]['metadata'] = {'speaker_voice_genders': hints}
        self.assertFalse(self._result(objects)['pass'])

    def test_wrong_role_gender_fails(self):
        hints = self._valid_hints()
        hints[0] = {'speaker': 'Frau', 'voice_gender': 'male'}
        objects = self._all_valid_objects()
        objects[0]['question_pairs'][0]['metadata'] = {'speaker_voice_genders': hints}
        self.assertFalse(self._result(objects)['pass'])

    def test_one_correct_pair_plus_unlabelled_pair_fails(self):
        objects = self._all_valid_objects()
        objects[1]['question_pairs'][0] = self._pair(
            'Ein Gespräch ohne erkennbare Sprecher.')
        self.assertFalse(self._result(objects)['pass'])

    def test_omitted_or_replaced_source_speaker_fails(self):
        objects = self._all_valid_objects()
        objects[0]['question_pairs'][0] = self._pair(
            'Frau : Guten Tag.\nHerr : Hallo.\nGast-Guten Morgen.',
            [
                {'speaker': 'Frau', 'voice_gender': 'female'},
                {'speaker': 'Herr', 'voice_gender': 'male'},
                {'speaker': 'Gast', 'voice_gender': 'unknown'},
            ],
        )
        self.assertFalse(self._result(objects)['pass'])

    def test_missing_expected_speaker_set_fails(self):
        objects = self._all_valid_objects()
        objects[1]['question_pairs'][0] = objects[0]['question_pairs'][0]
        self.assertFalse(self._result(objects)['pass'])

    def test_collapsing_two_fixture_editions_into_one_object_fails(self):
        objects = self._all_valid_objects()
        collapsed = [{
            'question_pairs': (
                objects[0]['question_pairs'] + objects[1]['question_pairs']
            ),
        }]
        self.assertFalse(self._result(collapsed)['pass'])

    def test_single_line_collapsed_speaker_turns_fail(self):
        objects = self._all_valid_objects()
        objects[0]['question_pairs'][0] = self._pair(
            'Frau: Guten Tag. Herr: Hallo. TN 1: Guten Morgen.',
            self._valid_hints(),
        )
        self.assertFalse(self._result(objects)['pass'])

    def test_expected_complete_edition_count_is_exact(self):
        self.assertTrue(self._result(self._all_valid_objects())['pass'])
        self.assertFalse(self._result(self._all_valid_objects()[:1])['pass'])

    def test_fabricated_placeholder_content_fails(self):
        objects = self._all_valid_objects()
        objects[0]['fabricated'] = 'placeholder'
        result = self._result(objects)
        self.assertFalse(result['pass'])
        self.assertIn('placeholder', result['reason'])

    def test_sole_source_header_normalizes_headerless_edition_number(self):
        objects = self._all_valid_objects()
        objects[1]['variant_number'] = 3
        objects[0]['variant_number'] = 2
        context = {'vars': {
            **self.context['vars'],
            'markdown': 'Hören Teil 1 (вариант №2)\n<<<ITEM>>>\nFortsetzung',
            'expected_variant_numbers': '[2, 2]',
        }}

        result = assertions.hoeren_teil1_speaker_voice_metadata_exact(
            _output(objects),
            context,
        )

        self.assertTrue(result['pass'], result['reason'])


class TelefonnotizVoiceMetadataAssertionTest(unittest.TestCase):
    context = {
        'vars': {
            'section_type': 'telefonnotiz',
            'expected_variant_number': 3,
            'expected_version_voice_genders': (
                '["male", "male", "male", "male", "male"]'
            ),
        }
    }

    def _result(self, genders, variant_number=3, extra_objects=None):
        versions = []
        for gender in genders:
            version = {'monologue': 'Guten Tag.'}
            if gender is not None:
                version['metadata'] = {'voice_gender': gender}
            versions.append(version)
        return assertions.telefonnotiz_nested_voice_metadata_matches_fixture(
            _output([
                {'variant_number': variant_number, 'versions': versions},
                *(extra_objects or []),
            ]), self.context
        )

    def test_valid_output_passes(self):
        self.assertTrue(
            self._result(['male', 'male', 'male', 'male', 'male'])['pass']
        )

    def test_missing_metadata_fails(self):
        self.assertFalse(
            self._result(['male', None, 'male', 'male', 'male'])['pass']
        )

    def test_wrong_gender_fails(self):
        self.assertFalse(
            self._result(['female', 'male', 'male', 'male', 'male'])['pass']
        )

    def test_wrong_order_fails(self):
        self.assertFalse(
            self._result(['male', 'male', 'female', 'male', 'male'])['pass']
        )

    def test_wrong_version_count_fails(self):
        self.assertFalse(self._result(['male', 'male', 'male', 'male'])['pass'])

    def test_split_slash_digits_must_not_be_concatenated(self):
        self.assertFalse(
            self._result(['male'] * 5, variant_number=31)['pass']
        )

    def test_editions_must_be_grouped_under_variant_three(self):
        self.assertFalse(self._result(
            ['male', 'male'],
            extra_objects=[{
                'variant_number': 3,
                'versions': [
                    {'monologue': 'Guten Tag.', 'metadata': {'voice_gender': 'male'}},
                    {'monologue': 'Guten Tag.', 'metadata': {'voice_gender': 'male'}},
                    {'monologue': 'Guten Tag.', 'metadata': {'voice_gender': 'male'}},
                ],
            }],
        )['pass'])


class LesenNegativeVoiceMetadataAssertionTest(unittest.TestCase):
    context = {'vars': {'section_type': 'lesen_teil1'}}

    def _result(self, text):
        return assertions.lesen_teil1_has_no_voice_metadata(
            _output([{'texts': [text]}]), self.context
        )

    def test_output_without_metadata_passes(self):
        self.assertTrue(self._result({'title': 'a', 'content': 'Text'})['pass'])

    def test_output_with_metadata_fails(self):
        self.assertFalse(
            self._result({
                'title': 'a',
                'content': 'Text',
                'metadata': {'voice_gender': 'female'},
            })['pass']
        )


class VoicePromptContractTest(unittest.TestCase):
    def test_h1_fragments_are_merged_without_placeholders(self):
        prompt = PROMPTS['hoeren_teil1']
        self.assertIn('marks source FRAGMENTS, not an object count', prompt)
        self.assertIn('never fabricate or write "placeholder"', prompt.lower())
        self.assertIn('Preserve a newline before EVERY labelled speaker turn', prompt)

    def test_telefonnotiz_split_slash_rule_preserves_base_variant(self):
        prompt = PROMPTS['telefonnotiz']
        self.assertIn('SPLIT-SLASH NUMBERING', prompt)
        self.assertIn('3/1 is variant 3, never variant 31', prompt)

    def test_voice_rules_identify_actual_speaker_without_fixture_names(self):
        for section_type in ('hoeren_teil4', 'telefonnotiz'):
            with self.subTest(section_type=section_type):
                prompt = PROMPTS[section_type]
                expected_role = (
                    'actual speaker'
                    if section_type == 'hoeren_teil4'
                    else 'actual caller'
                )
                self.assertIn(expected_role, prompt.lower())
                self.assertIn('merely mentioned', prompt)
                self.assertNotIn('Andrea', prompt)

        self.assertIn('self-identification', PROMPTS['telefonnotiz'])
        self.assertIn('matching answer block', PROMPTS['telefonnotiz'])
        self.assertIn('VOICE SPEAKER DISAMBIGUATION', PROMPTS['hoeren_teil4'])
        self.assertIn('matching numbered question', PROMPTS['hoeren_teil4'])

if __name__ == '__main__':
    unittest.main()

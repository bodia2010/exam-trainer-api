import unittest

from prompts import PROMPTS
from response_schemas import SPAN_TEXT_SECTION_TYPES, schema_for


def _text_item_schema(section_type):
    schema = schema_for(section_type)
    return schema['items']['properties']['texts']['items']


class SpanPromptSchemaContractTest(unittest.TestCase):
    def test_only_intended_sections_use_span_texts(self):
        self.assertEqual(
            SPAN_TEXT_SECTION_TYPES,
            {'lesen_teil2', 'hoeren_teil4'},
        )

    def test_span_sections_use_pointer_schema_and_strict_prompt_rules(self):
        for section_type in SPAN_TEXT_SECTION_TYPES:
            with self.subTest(section_type=section_type):
                text_item = _text_item_schema(section_type)
                self.assertIn('start_line', text_item['properties'])
                self.assertIn('end_line', text_item['properties'])
                self.assertNotIn('content', text_item['properties'])

                prompt = PROMPTS[section_type]
                self.assertIn('start_line must be less than or equal to end_line', prompt)
                self.assertIn('exact -1/-1 missing-text sentinel', prompt)

    def test_non_span_universal_section_keeps_legacy_content_contract(self):
        text_item = _text_item_schema('lesen_teil1')

        self.assertIn('content', text_item['properties'])
        self.assertNotIn('start_line', text_item['properties'])
        self.assertNotIn('missing-text sentinel', PROMPTS['lesen_teil1'])

    def test_optional_voice_metadata_schema_is_backward_compatible(self):
        for section_type in ['lesen_teil1', 'lesen_teil2', 'hoeren_teil4']:
            with self.subTest(section_type=section_type):
                text_item = _text_item_schema(section_type)
                metadata = text_item['properties']['metadata']

                self.assertNotIn('metadata', text_item['required'])
                self.assertEqual(
                    metadata['properties']['voice_gender']['enum'],
                    ['female', 'male', 'unknown'],
                )
                speaker_hint = metadata['properties']['speaker_voice_genders']['items']
                self.assertEqual(
                    speaker_hint['properties']['voice_gender']['enum'],
                    ['female', 'male', 'unknown'],
                )

    def test_bespoke_audio_schemas_permit_optional_voice_metadata(self):
        hoeren_pair = (
            schema_for('hoeren_teil1')['items']['properties']
            ['question_pairs']['items']
        )
        telefon_version = (
            schema_for('telefonnotiz')['items']['properties']['versions']
            ['items']
        )

        self.assertIn('metadata', hoeren_pair['properties'])
        self.assertNotIn('metadata', hoeren_pair['required'])
        self.assertIn('metadata', telefon_version['properties'])
        self.assertNotIn('metadata', telefon_version['required'])

    def test_prompts_constrain_voice_metadata_values(self):
        self.assertIn('voice_gender', PROMPTS['telefonnotiz'])
        self.assertIn('female|male|unknown', PROMPTS['telefonnotiz'])
        self.assertIn('speaker_voice_genders', PROMPTS['hoeren_teil1'])
        self.assertIn('Allowed gender values are exactly', PROMPTS['hoeren_teil4'])


if __name__ == '__main__':
    unittest.main()

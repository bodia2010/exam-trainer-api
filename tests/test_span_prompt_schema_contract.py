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


if __name__ == '__main__':
    unittest.main()

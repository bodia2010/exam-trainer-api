import unittest

import generation_config


class GenerationConfigThinkingLevelTest(unittest.TestCase):
    def test_light_parse_section_remains_minimal(self):
        config = generation_config.build(
            generation_config.model_for('lesen_teil1'),
            'lesen_teil1',
        )

        self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'MINIMAL'})
        self.assertEqual(config['temperature'], 0.2)

    def test_hoeren_teil1_and_existing_heavy_sections_use_low(self):
        self.assertIn('hoeren_teil1', generation_config.HEAVY_SECTION_TYPES)
        for section_type in generation_config.HEAVY_SECTION_TYPES:
            with self.subTest(section_type=section_type):
                config = generation_config.build(
                    generation_config.model_for(section_type),
                    section_type,
                )
                self.assertEqual(
                    config['thinkingConfig'],
                    {'thinkingLevel': 'LOW'},
                )
                self.assertEqual(config['temperature'], 0.2)

    def test_all_parse_sections_keep_cost_efficient_default_model(self):
        self.assertEqual(
            generation_config.model_for('hoeren_teil1'),
            generation_config.DEFAULT_MODEL,
        )
        for section_type in generation_config.HEAVY_SECTION_TYPES:
            with self.subTest(section_type=section_type):
                self.assertEqual(
                    generation_config.model_for(section_type),
                    generation_config.DEFAULT_MODEL,
                )

    def test_discovery_model_and_decoding_remain_unchanged(self):
        model = generation_config.model_for('discover')
        config = generation_config.build(model, 'discover')

        self.assertEqual(model, 'gemini-3.5-flash')
        self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'MINIMAL'})
        self.assertEqual(config['temperature'], 0)


if __name__ == '__main__':
    unittest.main()

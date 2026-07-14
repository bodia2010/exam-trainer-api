import unittest

import line_extraction


class NumberMarkdownTest(unittest.TestCase):
    def test_number_markdown_keeps_existing_zero_based_format(self):
        self.assertEqual(
            line_extraction.number_markdown('alpha\n\nbeta'),
            '00000: alpha\n00001: \n00002: beta',
        )


class ExtractSpanValidationTest(unittest.TestCase):
    def setUp(self):
        self.raw_lines = ['first', 'second-', 'half', 'last']

    def test_extract_span_accepts_first_and_last_boundaries(self):
        self.assertEqual(
            line_extraction.extract_span(self.raw_lines, 0, 0),
            'first',
        )
        self.assertEqual(
            line_extraction.extract_span(self.raw_lines, 3, 3),
            'last',
        )

    def test_extract_span_keeps_existing_valid_formatting(self):
        self.assertEqual(
            line_extraction.extract_span(self.raw_lines, 1, 2),
            'secondhalf',
        )

    def test_extract_span_rejects_inverted_span(self):
        with self.assertRaises(ValueError):
            line_extraction.extract_span(self.raw_lines, 2, 1)

    def test_extract_span_rejects_out_of_range_span(self):
        with self.assertRaises(ValueError):
            line_extraction.extract_span(self.raw_lines, 0, len(self.raw_lines))

    def test_extract_span_rejects_negative_span(self):
        with self.assertRaises(ValueError):
            line_extraction.extract_span(self.raw_lines, -1, 0)

    def test_extract_span_rejects_missing_span_sentinel(self):
        self.assertTrue(line_extraction.is_missing_span_sentinel(-1, -1))
        with self.assertRaises(ValueError):
            line_extraction.extract_span(self.raw_lines, -1, -1)


class ExtractBlockTest(unittest.TestCase):
    def test_extract_block_keeps_existing_multiline_formatting(self):
        raw_lines = [
            '  Kapitel 1  ',
            '',
            '',
            'Silben-',
            'trennung',
            'Sprecher: Hallo  ',
            '',
        ]

        self.assertEqual(
            line_extraction.extract_block(raw_lines, 0, 6, heading_lines=[0]),
            '**Kapitel 1**\n\nSilbentrennung\nSprecher: Hallo',
        )

    def test_extract_block_rejects_heading_lines_before_span(self):
        with self.assertRaises(ValueError):
            line_extraction.extract_block(
                ['title', 'body'],
                1,
                1,
                heading_lines=[0],
            )

    def test_extract_block_rejects_heading_lines_after_span(self):
        with self.assertRaises(ValueError):
            line_extraction.extract_block(
                ['title', 'body'],
                0,
                0,
                heading_lines=[1],
            )

    def test_extract_block_rejects_non_list_heading_lines(self):
        with self.assertRaises(TypeError):
            line_extraction.extract_block(
                ['title', 'body'],
                0,
                1,
                heading_lines=(0,),
            )

    def test_extract_block_rejects_non_integer_heading_line(self):
        with self.assertRaises(TypeError):
            line_extraction.extract_block(
                ['title', 'body'],
                0,
                1,
                heading_lines=['0'],
            )


if __name__ == '__main__':
    unittest.main()

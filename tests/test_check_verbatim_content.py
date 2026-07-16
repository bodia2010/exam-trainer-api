import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / 'scripts' / 'check_verbatim_content.py'
)
SPEC = importlib.util.spec_from_file_location(
    'exam_trainer_check_verbatim_content', MODULE_PATH
)
check_verbatim_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_verbatim_content)


class SlashEditionAlternativeTest(unittest.TestCase):
    def test_strips_option_marker_and_score_suffix(self):
        self.assertEqual(
            [
                'verlangt noch einige unterlagen',
                'verlangt noch weitere unterlagen von herrn klein',
            ],
            check_verbatim_content._slash_alternatives(
                'c) verlangt noch einige Unterlagen. / '
                'verlangt noch weitere Unterlagen von Herrn Klein. – 100%'
            ),
        )

    def test_accepts_alternative_embedded_in_reviewed_multi_option_field(self):
        self.assertTrue(check_verbatim_content._has_sibling_alternative(
            'schnellhefter abschicken',
            {'sofort fehlende schnellhefter abschicken'},
        ))

    def test_does_not_match_short_generic_fragment(self):
        self.assertFalse(check_verbatim_content._has_sibling_alternative(
            'morgen',
            {'bis morgen abschicken'},
        ))


if __name__ == '__main__':
    unittest.main()

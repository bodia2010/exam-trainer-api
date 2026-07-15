import importlib.util
import json
from pathlib import Path
import unittest


ASSERTIONS_PATH = (
    Path(__file__).resolve().parents[1] / 'promptfoo' / 'assertions.py'
)
SPEC = importlib.util.spec_from_file_location(
    'exam_trainer_promptfoo_count_assertions', ASSERTIONS_PATH
)
promptfoo_assertions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promptfoo_assertions)


class ItemCountExactlyWhenSetTest(unittest.TestCase):
    def _result(self, count, expected_items=None):
        output = json.dumps([
            {'variant_number': 1, 'version': None}
            for _ in range(count)
        ])
        variables = {'section_type': 'hoeren_teil4'}
        if expected_items is not None:
            variables['expected_items'] = expected_items
        return promptfoo_assertions.item_count_exactly_when_set(
            output,
            {'vars': variables},
        )

    def test_is_no_op_without_expected_items(self):
        result = self._result(3)
        self.assertTrue(result['pass'], result['reason'])

    def test_accepts_exact_count(self):
        result = self._result(1, expected_items=1)
        self.assertTrue(result['pass'], result['reason'])

    def test_rejects_both_missing_and_extra_objects(self):
        for count in (0, 2):
            with self.subTest(count=count):
                result = self._result(count, expected_items=1)
                self.assertFalse(result['pass'], result['reason'])
                self.assertEqual(result['score'], 0)


if __name__ == '__main__':
    unittest.main()

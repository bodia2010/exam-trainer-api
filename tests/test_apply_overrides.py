import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'tools' / 'apply_overrides.py'
SPEC = importlib.util.spec_from_file_location('exam_trainer_apply_overrides', MODULE_PATH)
apply_overrides = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(apply_overrides)


class ApplyOverridesTest(unittest.TestCase):
    def setUp(self):
        self.course = {
            'id': 'course-id',
            'sections': {
                'hoeren_teil4': [{
                    'variant_number': 8,
                    'texts': [{'title': 'A', 'content': 'hallucinated'}],
                    'questions': [
                        {'number': 36, 'answer': 'a'},
                        {'number': 37, 'answer': 'b'},
                    ],
                }],
                'telefonnotiz': [{
                    'variant_number': 3,
                    'versions': [
                        {'label': 'Alte Version', 'answer': {'name': 'Eva'}},
                        {'label': 'Neue Version', 'answer': {'name': 'Mia'}},
                    ],
                }],
            },
        }

    def _patch(self, **updates):
        patch = {
            'section': 'hoeren_teil4',
            'variant': 8,
            'path': 'questions[number=36].answer',
            'old': 'a',
            'new': 'c',
            'reason': 'PDF highlight marks c',
        }
        patch.update(updates)
        return patch

    def test_applies_stable_selector_without_mutating_input(self):
        result = apply_overrides.apply_overrides(self.course, [self._patch()])

        self.assertEqual(result['sections']['hoeren_teil4'][0]['questions'][0]['answer'], 'c')
        self.assertEqual(self.course['sections']['hoeren_teil4'][0]['questions'][0]['answer'], 'a')

    def test_applies_numeric_index_and_unquoted_string_selector(self):
        patches = [
            self._patch(path='texts[0].content', old='hallucinated',
                        new='(nicht angegeben)'),
            self._patch(section='telefonnotiz', variant=3,
                        path='versions[label=Neue Version].answer.name',
                        old='Mia', new='Maria'),
        ]

        result = apply_overrides.apply_overrides(self.course, patches)

        self.assertEqual(
            result['sections']['hoeren_teil4'][0]['texts'][0]['content'],
            '(nicht angegeben)',
        )
        self.assertEqual(
            result['sections']['telefonnotiz'][0]['versions'][1]['answer']['name'],
            'Maria',
        )

    def test_fails_closed_when_old_value_drifted(self):
        with self.assertRaisesRegex(ValueError, 'old value mismatch'):
            apply_overrides.apply_overrides(
                self.course,
                [self._patch(old='stale answer')],
            )

    def test_rejects_ambiguous_variant_and_duplicate_target(self):
        self.course['sections']['hoeren_teil4'].append({'variant_number': 8})
        with self.assertRaisesRegex(ValueError, 'matched 2 entries'):
            apply_overrides.apply_overrides(self.course, [self._patch()])

        self.course['sections']['hoeren_teil4'].pop()
        with self.assertRaisesRegex(ValueError, 'duplicate override target'):
            apply_overrides.apply_overrides(self.course, [self._patch(), self._patch()])

    def test_removes_whole_item_only_with_exact_old_object(self):
        old_item = self.course['sections']['hoeren_teil4'][0]
        patch = self._patch(path='<item>', old=old_item, new=None)

        result = apply_overrides.apply_overrides(self.course, [patch])

        self.assertEqual(result['sections']['hoeren_teil4'], [])

    def test_rejects_missing_reason_and_path_escape(self):
        with self.assertRaisesRegex(ValueError, 'reason must be a non-empty string'):
            apply_overrides.apply_overrides(self.course, [self._patch(reason='')])
        with self.assertRaisesRegex(ValueError, 'invalid override path segment'):
            apply_overrides.apply_overrides(self.course, [self._patch(path='questions..answer')])
        with self.assertRaisesRegex(ValueError, 'variant must be an integer'):
            apply_overrides.apply_overrides(self.course, [self._patch(variant=True)])


if __name__ == '__main__':
    unittest.main()

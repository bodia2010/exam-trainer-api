import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'update_curated_content.py'
SPEC = importlib.util.spec_from_file_location(
    'exam_trainer_update_curated_content', MODULE_PATH)
update_curated_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_curated_content)


def _course(*items):
    return {'lesen_teil2': list(items)}


class UpdateCuratedContentIdentityTest(unittest.TestCase):
    def test_reuses_an_unchanged_item(self):
        item = {'variant_number': 1, 'version': None, 'topic': 'Arbeitsalltag'}

        reused, changed = update_curated_content.diff_courses(
            _course(item), _course(dict(item)))

        self.assertEqual([('lesen_teil2', item)], reused)
        self.assertEqual([], changed)

    def test_rejects_duplicate_identity_in_old_course(self):
        with self.assertRaisesRegex(
            ValueError,
            r"duplicate identity in old course: section_type='lesen_teil2', "
            r"variant_number=1, version='neu'",
        ):
            update_curated_content.diff_courses(
                _course(
                    {'variant_number': 1, 'version': ' Neu '},
                    {'variant_number': 1, 'version': 'neu'},
                ),
                _course({'variant_number': 1}),
            )

    def test_rejects_duplicate_identity_in_new_course(self):
        with self.assertRaisesRegex(
            ValueError,
            r"duplicate identity in new course: section_type='lesen_teil2', "
            r"variant_number=1, version='<original>'",
        ):
            update_curated_content.diff_courses(
                _course({'variant_number': 1}),
                _course(
                    {'variant_number': 1, 'version': None},
                    {'variant_number': 1, 'version': ''},
                ),
            )

    def test_rejects_a_single_missing_or_invalid_identity(self):
        for item, expected in (
            ({'version': None}, 'variant_number must be an integer'),
            ({'variant_number': True}, 'variant_number must be an integer'),
            ({'variant_number': 1, 'version': 7}, 'version must be a string or null'),
        ):
            with self.subTest(item=item):
                with self.assertRaisesRegex(ValueError, expected):
                    update_curated_content.diff_courses(_course(item), _course(item))

    def test_rejects_missing_old_identity_in_new_course(self):
        old = _course(
            {'variant_number': 1},
            {'variant_number': 2},
        )
        new = _course({'variant_number': 1})

        with self.assertRaisesRegex(ValueError, 'missing 1 old identity'):
            update_curated_content.diff_courses(old, new)

    def test_rejects_empty_or_malformed_course_shapes(self):
        malformed = (
            {},
            {'lesen_teil2': {}},
            {'lesen_teil2': ['not an object']},
        )
        for course in malformed:
            with self.subTest(course=course):
                with self.assertRaises(ValueError):
                    update_curated_content.diff_courses(
                        _course({'variant_number': 1}),
                        course,
                    )


if __name__ == '__main__':
    unittest.main()

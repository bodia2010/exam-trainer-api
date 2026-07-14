import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import inject_curated


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes = []

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value


class CuratedCacheKeyTest(unittest.TestCase):
    def test_document_key_matches_flutter_contract(self):
        markdown = 'eins\nzwei'
        expected = hashlib.sha256('doc|eins\nzwei'.encode()).hexdigest()
        self.assertEqual(expected, inject_curated.document_digest(markdown))
        self.assertEqual(
            f'v30.v35|doc|{expected}',
            inject_curated.document_key('v30', 'v35', expected),
        )

    def test_full_course_json_serializes_only_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'course.json'
            path.write_text(json.dumps({
                'id': 'ignored',
                'sections': {'lesen_teil2': [{'variant_number': 1}]},
            }), encoding='utf-8')
            value = inject_curated.serialized_sections(path)
        self.assertEqual(
            {'lesen_teil2': [{'variant_number': 1}]}, json.loads(value))

    def test_invalid_section_shape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must be a list'):
            inject_curated.validate_sections({'lesen_teil2': {}})

    def test_pdf_conversion_fails_closed_without_marker_dependency(self):
        real_import = __import__

        def import_without_fitz(name, *args, **kwargs):
            if name == 'fitz':
                raise ImportError('missing in test')
            return real_import(name, *args, **kwargs)

        with mock.patch('builtins.__import__', side_effect=import_without_fitz):
            with self.assertRaisesRegex(RuntimeError, 'PyMuPDF is required'):
                inject_curated.convert_pdf_to_markdown(Path('/unused.pdf'))

    def test_legacy_source_key_can_be_supplied_when_conversion_hash_changed(self):
        args = inject_curated.parse_args([
            '--pdf', '/tmp/source.pdf',
            '--source-key', 'v30.v32|doc|legacy-hash',
        ])
        self.assertEqual('v30.v32|doc|legacy-hash', args.source_key)


class CuratedMigrationTest(unittest.TestCase):
    def setUp(self):
        self.value = json.dumps({'lesen_teil2': [{'variant_number': 1}]})

    def test_dry_run_does_not_write(self):
        redis = FakeRedis({'old': self.value})
        result = inject_curated.migrate(
            redis, 'old', 'new', supplied_value=None, apply=False)
        self.assertEqual('dry-run', result)
        self.assertEqual([], redis.writes)

    def test_apply_writes_and_verifies(self):
        redis = FakeRedis({'old': self.value})
        result = inject_curated.migrate(
            redis, 'old', 'new', supplied_value=None, apply=True)
        self.assertEqual('written', result)
        self.assertEqual([('new', self.value)], redis.writes)

    def test_matching_target_is_safe_noop(self):
        redis = FakeRedis({'old': self.value, 'new': self.value})
        result = inject_curated.migrate(
            redis, 'old', 'new', supplied_value=None, apply=True)
        self.assertEqual('already-current', result)
        self.assertEqual([], redis.writes)

    def test_different_target_is_never_overwritten(self):
        other = json.dumps({'lesen_teil2': [{'variant_number': 2}]})
        redis = FakeRedis({'old': self.value, 'new': other})
        with self.assertRaisesRegex(RuntimeError, 'refusing to overwrite'):
            inject_curated.migrate(
                redis, 'old', 'new', supplied_value=None, apply=True)
        self.assertEqual([], redis.writes)

    def test_readback_mismatch_fails(self):
        redis = mock.Mock()
        redis.get.side_effect = [self.value, None, '{}']
        with self.assertRaisesRegex(RuntimeError, 'read-back'):
            inject_curated.migrate(
                redis, 'old', 'new', supplied_value=None, apply=True)


if __name__ == '__main__':
    unittest.main()

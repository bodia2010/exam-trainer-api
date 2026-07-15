import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import inject_curated
from tools import curation_receipt


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

    def test_course_apply_requires_matching_checklist_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / 'source.pdf'
            pdf.write_bytes(b'pdf bytes')
            course = root / 'course.json'
            course.write_text(json.dumps({
                'sections': {'lesen_teil2': [{'variant_number': 1}]},
            }), encoding='utf-8')

            with mock.patch.object(inject_curated, 'convert_pdf_to_markdown', return_value='md'):
                with self.assertRaisesRegex(SystemExit, '--checklist-receipt, --checklist-report'):
                    inject_curated.main([
                        '--pdf', str(pdf), '--course', str(course), '--apply',
                    ])

            value = inject_curated.serialized_sections(course)
            receipt = curation_receipt.build_receipt(
                course_value=value,
                pdf_bytes=pdf.read_bytes(),
                source_markdown_bytes=b'md',
                report_text='reviewed',
                review_items=1,
                deterministic_findings=0,
                llm_findings=0,
                checks={
                    'diff': 'completed', 'answer_keys': 'completed',
                    'verbatim': 'completed', 'llm': 'skipped-missing-api-key',
                },
            )
            receipt_path = root / 'receipt.json'
            curation_receipt.write_receipt(receipt_path, receipt)
            report_path = root / 'report.txt'
            report_path.write_text('reviewed', encoding='utf-8')
            source_md = root / 'source.md'
            source_md.write_bytes(b'md')
            with mock.patch.object(inject_curated, 'convert_pdf_to_markdown', return_value='md'):
                with mock.patch.dict('os.environ', {}, clear=True):
                    with self.assertRaisesRegex(SystemExit, 'Redis credentials are required'):
                        inject_curated.main([
                            '--pdf', str(pdf), '--course', str(course), '--apply',
                            '--checklist-receipt', str(receipt_path),
                            '--checklist-report', str(report_path),
                            '--checklist-source-md', str(source_md),
                        ])

    def test_receipt_rejects_different_course_or_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receipt.json'
            receipt = curation_receipt.build_receipt(
                course_value='{"a":[]}',
                pdf_bytes=b'pdf-a',
                source_markdown_bytes=b'md',
                report_text='report',
                review_items=0,
                deterministic_findings=0,
                llm_findings=0,
                checks={
                    'diff': 'completed', 'answer_keys': 'completed',
                    'verbatim': 'completed', 'llm': 'skipped-missing-api-key',
                },
            )
            curation_receipt.write_receipt(path, receipt)
            with self.assertRaisesRegex(ValueError, 'course hash'):
                curation_receipt.verify_receipt(
                    path, course_value='{"b":[]}', pdf_bytes=b'pdf-a',
                    source_markdown_bytes=b'md', report_text='report')
            with self.assertRaisesRegex(ValueError, 'PDF hash'):
                curation_receipt.verify_receipt(
                    path, course_value='{"a":[]}', pdf_bytes=b'pdf-b',
                    source_markdown_bytes=b'md', report_text='report')
            receipt['checks']['answer_keys'] = 'skipped-missing-pymupdf'
            curation_receipt.write_receipt(path, receipt)
            with self.assertRaisesRegex(ValueError, 'answer_keys did not complete'):
                curation_receipt.verify_receipt(
                    path, course_value='{"a":[]}', pdf_bytes=b'pdf-a',
                    source_markdown_bytes=b'md', report_text='report')


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

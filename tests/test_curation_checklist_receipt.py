import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tools import curation_receipt, inject_curated


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'curation_checklist.py'
SPEC = importlib.util.spec_from_file_location('exam_trainer_curation_checklist', MODULE_PATH)
curation_checklist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curation_checklist)


class CurationChecklistReceiptTest(unittest.TestCase):
    def test_unchanged_course_emits_receipt_bound_to_course_and_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            course = root / 'course.json'
            course.write_text(json.dumps({
                'sections': {'lesen_teil1': [{'variant_number': 1}]},
            }), encoding='utf-8')
            pdf = root / 'source.pdf'
            pdf.write_bytes(b'pdf source')
            source_md = root / 'source.md'
            source_md.write_bytes(b'\x0cmarkdown source\n')
            report = root / 'report.txt'
            receipt_path = root / 'receipt.json'

            argv = [
                'curation_checklist.py',
                '--old-course', str(course),
                '--new-course', str(course),
                '--pdf', str(pdf),
                '--source-md', str(source_md),
                '--out', str(report),
                '--receipt', str(receipt_path),
            ]
            with mock.patch.object(sys, 'argv', argv):
                with mock.patch.dict('os.environ', {}, clear=True):
                    curation_checklist.main()

            receipt = curation_receipt.verify_receipt(
                receipt_path,
                course_value=inject_curated.serialized_sections(course),
                pdf_bytes=pdf.read_bytes(),
                source_markdown_bytes=source_md.read_bytes(),
                report_text=report.read_text(encoding='utf-8'),
            )
            self.assertEqual(0, receipt['review_items'])
            self.assertEqual('not-needed', receipt['checks']['answer_keys'])
            self.assertTrue(report.read_text(encoding='utf-8').startswith('# Curation checklist'))


if __name__ == '__main__':
    unittest.main()

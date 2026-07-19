import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'offline_semantic_gate.py'
SPEC = importlib.util.spec_from_file_location('exam_trainer_offline_semantic_gate', MODULE_PATH)
offline_semantic_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline_semantic_gate)


def _course(*items):
    return {'sections': {'synthetic': list(items)}}


def _item(variant=1, *, version=None, question='synthetic question', metadata=None):
    value = {
        'variant_number': variant,
        'version': version,
        'questions': [{'number': 1, 'text': question, 'answer': 'a'}],
    }
    if metadata is not None:
        value['metadata'] = metadata
    return value


class OfflineSemanticGateTest(unittest.TestCase):
    def test_identical_courses_pass(self):
        report = offline_semantic_gate.compare_courses(_course(_item()), _course(_item()))
        self.assertEqual(1, report['metrics']['exact'])
        self.assertEqual([], offline_semantic_gate.evaluate_gate(report))

    def test_metadata_only_difference_is_visible_and_fails_by_default(self):
        trusted = _course(_item(metadata={'voice_gender': 'female'}))
        fresh = _course(_item(metadata={'voice_gender': 'male'}))
        report = offline_semantic_gate.compare_courses(trusted, fresh)
        self.assertEqual(1, report['metrics']['metadata_only'])
        self.assertEqual(['1 voice metadata drift'], offline_semantic_gate.evaluate_gate(report))
        self.assertEqual([], offline_semantic_gate.evaluate_gate(report, allow_metadata_drift=True))

    def test_audio_url_is_payload_drift_even_with_voice_metadata_policy(self):
        trusted = _course(_item())
        fresh = copy.deepcopy(trusted)
        fresh['sections']['synthetic'][0]['audio_url'] = 'https://example.test/new.mp3'
        report = offline_semantic_gate.compare_courses(trusted, fresh)
        self.assertEqual(1, report['metrics']['payload_changed'])
        self.assertEqual(['1 exercise payload drift'], offline_semantic_gate.evaluate_gate(
            report, allow_metadata_drift=True))

    def test_non_voice_metadata_is_payload_drift_even_with_voice_policy(self):
        trusted = _course(_item(metadata={
            'voice_gender': 'female',
            'content_label': 'authoritative',
        }))
        fresh = _course(_item(metadata={
            'voice_gender': 'male',
            'content_label': 'changed',
        }))

        report = offline_semantic_gate.compare_courses(trusted, fresh)

        self.assertEqual(1, report['metrics']['payload_changed'])
        self.assertNotEqual(
            [],
            offline_semantic_gate.evaluate_gate(
                report,
                allow_metadata_drift=True,
            ),
        )

    def test_empty_section_name_drift_fails(self):
        trusted = _course(_item())
        fresh = _course(_item())
        fresh['sections']['unexpected'] = []

        report = offline_semantic_gate.compare_courses(trusted, fresh)

        self.assertEqual(['unexpected'], report['fresh_only_sections'])
        self.assertIn(
            '1 fresh-only section',
            offline_semantic_gate.evaluate_gate(report),
        )

    def test_payload_difference_fails_even_with_metadata_policy(self):
        trusted = _course(_item(question='trusted question'))
        fresh = _course(_item(question='different question'))
        report = offline_semantic_gate.compare_courses(trusted, fresh)
        self.assertEqual(1, report['metrics']['payload_changed'])
        self.assertEqual(['1 exercise payload drift'], offline_semantic_gate.evaluate_gate(
            report, allow_metadata_drift=True))

    def test_extra_and_missing_items_fail(self):
        trusted = _course(_item(1), _item(2))
        fresh = _course(_item(1), _item(3))
        report = offline_semantic_gate.compare_courses(trusted, fresh)
        self.assertEqual(1, report['metrics']['fresh_only'])
        self.assertEqual(1, report['metrics']['trusted_only'])
        self.assertFalse(report['metrics']['identity_set_equal'])
        self.assertEqual(
            ['1 fresh-only identity', '1 trusted-only identity'],
            offline_semantic_gate.evaluate_gate(report),
        )

    def test_mixed_unversioned_and_versioned_identities_are_sorted_without_crashing(self):
        trusted = _course(_item(1), _item(1, version='Neue Version'))
        fresh = _course(_item(1), _item(1, version='Alte Version'))
        report = offline_semantic_gate.compare_courses(trusted, fresh)
        self.assertEqual(1, report['metrics']['exact'])
        self.assertEqual(1, report['metrics']['fresh_only'])
        self.assertEqual('Alte Version', report['fresh_only'][0]['version'])
        self.assertEqual(1, report['metrics']['trusted_only'])
        self.assertEqual('Neue Version', report['trusted_only'][0]['version'])

    def test_duplicate_identity_is_not_silently_overwritten(self):
        trusted = _course(_item(1), _item(1, question='duplicate'))
        report = offline_semantic_gate.compare_courses(trusted, _course(_item(1)))
        self.assertEqual(1, report['metrics']['duplicate_identities'])
        self.assertEqual([0, 1], report['duplicate_identities'][0]['item_indexes'])
        self.assertIn('duplicate identity', offline_semantic_gate.evaluate_gate(report)[0])

    def test_missing_variant_number_fails(self):
        bad = _item()
        del bad['variant_number']
        report = offline_semantic_gate.compare_courses(_course(bad), _course(bad))
        self.assertEqual(2, report['metrics']['missing_identity'])
        self.assertTrue(offline_semantic_gate.evaluate_gate(report))

    def test_empty_courses_are_rejected_before_a_false_pass(self):
        with self.assertRaisesRegex(ValueError, 'at least one item'):
            offline_semantic_gate.compare_courses({'sections': {}}, {'sections': {}})

    def test_cli_writes_machine_readable_report_and_obeys_explicit_policy(self):
        trusted = _course(_item(metadata={'voice_gender': 'female'}))
        fresh = copy.deepcopy(trusted)
        fresh['sections']['synthetic'][0]['metadata']['voice_gender'] = 'male'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted_path = root / 'trusted.json'
            fresh_path = root / 'fresh.json'
            report_path = root / 'nested' / 'report.json'
            trusted_path.write_text(json.dumps(trusted), encoding='utf-8')
            fresh_path.write_text(json.dumps(fresh), encoding='utf-8')

            self.assertEqual(1, offline_semantic_gate.main([
                '--trusted', str(trusted_path), '--fresh', str(fresh_path), '--report', str(report_path),
            ]))
            result = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertFalse(result['gate']['passed'])
            self.assertFalse(result['policy']['allow_metadata_drift'])

            self.assertEqual(0, offline_semantic_gate.main([
                '--trusted', str(trusted_path), '--fresh', str(fresh_path), '--report', str(report_path),
                '--allow-metadata-drift',
            ]))
            result = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertTrue(result['gate']['passed'])
            self.assertTrue(result['policy']['allow_metadata_drift'])

    def test_cli_writes_fail_closed_report_for_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted_path = root / 'trusted.json'
            fresh_path = root / 'fresh.json'
            report_path = root / 'report.json'
            trusted_path.write_text('{not JSON', encoding='utf-8')
            fresh_path.write_text(json.dumps(_course(_item())), encoding='utf-8')

            self.assertEqual(2, offline_semantic_gate.main([
                '--trusted', str(trusted_path), '--fresh', str(fresh_path), '--report', str(report_path),
            ]))
            result = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertFalse(result['gate']['passed'])
            self.assertEqual(['invalid input'], result['gate']['failures'])


if __name__ == '__main__':
    unittest.main()

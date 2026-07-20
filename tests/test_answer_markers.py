import json
import unittest
from unittest.mock import patch

import answer_markers
from prompts import PROMPTS

try:
    import main
except ModuleNotFoundError as error:
    # The pure marker regressions deliberately run in the lightweight Python
    # environment too.  Endpoint coverage runs wherever the backend runtime
    # dependencies are installed (the normal CI/deploy environment).
    if error.name not in {'flask', 'pdfminer', 'requests'}:
        raise
    main = None


def _choice(answer='c', options=None):
    return [{
        'variant_number': 1,
        'questions': [{
            'number': 1,
            'type': 'choice',
            'text': 'Question',
            'answer': answer,
            'options': options or [
                {'letter': 'a', 'text': 'alpha answer'},
                {'letter': 'b', 'text': 'beta answer'},
                {'letter': 'c', 'text': 'gamma answer'},
            ],
        }],
    }]


class PdfCorrectMarkerTest(unittest.TestCase):
    def test_prompts_define_physical_marker_precedence_and_no_leakage(self):
        prompt = PROMPTS['beschwerde']

        self.assertIn('[[PDF_CORRECT: <option text>]]', prompt)
        self.assertIn('Never copy this technical marker into JSON fields.', prompt)
        self.assertIn('secondary to ``PDF_CORRECT``', prompt)

    def test_two_column_existing_text_marker_does_not_suppress_pdf_marker(self):
        answer = 'die Mitarbeiter der Firma.'
        markdown = (
            'a) die Mitarbeiter der Firma. '
            'a) haben vier Möbelpacker gearbeitet. – 100%\n'
        )
        with patch.object(
            answer_markers,
            'highlighted_options',
            return_value={answer_markers._normalize(answer): [(1, f'a) {answer}')]},
        ):
            actual = answer_markers._inject_answer_markers('fixture.pdf', markdown)

        self.assertIn(
            '[[PDF_CORRECT:die mitarbeiter der firma]]',
            actual,
        )
        self.assertIn('haben vier Möbelpacker gearbeitet. – 100%', actual)

    def test_legacy_injector_keeps_deployed_first_match_and_marker_shape(self):
        answer = 'gleiche Antwort'
        markdown = 'a) gleiche Antwort\nb) gleiche Antwort\n'
        with patch.object(
            answer_markers,
            'highlighted_options',
            return_value={answer_markers._normalize(answer): [(1, f'a) {answer}')]},
        ):
            actual = answer_markers._inject_legacy_answer_markers(
                'fixture.pdf',
                markdown,
            )

        self.assertEqual('a) gleiche Antwort – 100%\nb) gleiche Antwort\n', actual)

    def test_strict_injection_propagates_extraction_failure_for_migration(self):
        for injector in (
            answer_markers._inject_legacy_answer_markers,
            answer_markers._inject_answer_markers,
        ):
            with self.subTest(injector=injector.__name__), patch.object(
                answer_markers,
                'highlighted_options',
                side_effect=ValueError('broken PDF geometry'),
            ):
                with self.assertRaisesRegex(RuntimeError, 'extraction failed'):
                    injector('fixture.pdf', 'a) answer', strict=True)

    def test_physical_marker_repairs_conflicting_textual_marker(self):
        options = [
            {'letter': 'a', 'text': 'führte zu Unzufriedenheit bei der Kundschaft.'},
            {'letter': 'b', 'text': 'soll als Modell für die neue Filiale dienen.'},
            {'letter': 'c', 'text': 'wird zurzeit neu ausgearbeitet.'},
        ]
        parsed = _choice(options=options)
        markdown = (
            'a) führte zu Unzufriedenheit bei der Kundschaft. '
            '[[PDF_CORRECT:führte zu unzufriedenheit bei der kundschaft]]\n'
            'c) wird zurzeit neu ausgearbeitet. – 100%\n'
        )

        self.assertEqual(1, answer_markers.repair_answers_from_pdf_markers(parsed, markdown))
        self.assertEqual('a', parsed[0]['questions'][0]['answer'])

    def test_repeated_option_text_with_one_pdf_hit_fails_closed(self):
        answer = 'gleiche Antwort'
        markdown = 'a) gleiche Antwort\nb) gleiche Antwort\n'
        with patch.object(
            answer_markers,
            'highlighted_options',
            return_value={answer_markers._normalize(answer): [(1, f'a) {answer}')]},
        ):
            actual = answer_markers._inject_answer_markers('fixture.pdf', markdown)

        self.assertNotIn('PDF_CORRECT', actual)

    def test_q55_inline_answer_is_repaired_from_physical_marker(self):
        options = [
            {'letter': 'a', 'text': 'Mittlerweile befinden'},
            {'letter': 'b', 'text': 'Zurzeit haben'},
            {'letter': 'c', 'text': 'Zwischenzeitlich sind'},
        ]
        parsed = _choice(options=options)
        markdown = (
            '55 (a - Mittlerweile befinden) wir uns in KW 18. '
            '[[PDF_CORRECT:mittlerweile befinden]]\n'
        )

        self.assertEqual(1, answer_markers.repair_answers_from_pdf_markers(parsed, markdown))
        self.assertEqual('a', parsed[0]['questions'][0]['answer'])

    def test_missing_malformed_or_ambiguous_markers_are_noops(self):
        for markdown in [
            'no provenance marker',
            '[[PDF_CORRECT: ]]',
            '[[PDF_CORRECT:alpha answer]] [[PDF_CORRECT:beta answer]]',
        ]:
            with self.subTest(markdown=markdown):
                parsed = _choice()
                self.assertEqual(
                    0,
                    answer_markers.repair_answers_from_pdf_markers(parsed, markdown),
                )
                self.assertEqual('c', parsed[0]['questions'][0]['answer'])

    def test_same_authoritative_text_on_two_options_is_ambiguous(self):
        parsed = _choice(options=[
            {'letter': 'a', 'text': 'gleiche Antwort'},
            {'letter': 'b', 'text': 'gleiche Antwort'},
            {'letter': 'c', 'text': 'dritte Antwort'},
        ])

        self.assertEqual(
            0,
            answer_markers.repair_answers_from_pdf_markers(
                parsed,
                '[[PDF_CORRECT:gleiche antwort]]',
            ),
        )
        self.assertEqual('c', parsed[0]['questions'][0]['answer'])

    def test_marker_text_reused_by_another_question_is_not_repaired(self):
        parsed = _choice(options=[
            {'letter': 'a', 'text': 'shared answer'},
            {'letter': 'b', 'text': 'first distractor'},
            {'letter': 'c', 'text': 'other distractor'},
        ])
        parsed[0]['questions'].append({
            'number': 2,
            'answer': 'b',
            'options': [
                {'letter': 'a', 'text': 'second distractor'},
                {'letter': 'b', 'text': 'shared answer'},
            ],
        })

        self.assertEqual(
            0,
            answer_markers.repair_answers_from_pdf_markers(
                parsed,
                '[[PDF_CORRECT:shared answer]]',
            ),
        )
        self.assertEqual(['c', 'b'], [q['answer'] for q in parsed[0]['questions']])

    def test_marker_leakage_is_removed_without_changing_other_strings(self):
        parsed = [{
            'texts': [{'content': 'Hallo [[PDF_CORRECT:alpha answer]]'}],
            'questions': [{
                'options': [{'letter': 'a', 'text': 'alpha answer [[PDF_CORRECT:alpha answer]]'}],
            }],
        }]

        cleaned = answer_markers.strip_pdf_correct_markers(parsed)

        self.assertEqual('Hallo', cleaned[0]['texts'][0]['content'])
        self.assertEqual('alpha answer', cleaned[0]['questions'][0]['options'][0]['text'])


class SprachbausteineInlineAnswerTest(unittest.TestCase):
    def _q55(self, answer='c'):
        parsed = _choice(answer=answer, options=[
            {'letter': 'a', 'text': 'Mittlerweile befinden'},
            {'letter': 'b', 'text': 'Zurzeit haben'},
            {'letter': 'c', 'text': 'Zwischenzeitlich sind'},
        ])
        parsed[0]['questions'][0]['number'] = 55
        return parsed

    def test_unique_inline_key_repairs_answer(self):
        parsed = self._q55()

        repaired = answer_markers.repair_sprachbausteine_inline_answers(
            parsed,
            '55 (a - Mittlerweile befinden) wir uns in KW 18.',
        )

        self.assertEqual(1, repaired)
        self.assertEqual('a', parsed[0]['questions'][0]['answer'])

    def test_agreeing_edition_chunks_repair_both_items(self):
        parsed = self._q55() + self._q55()
        markdown = (
            '55 (a - Mittlerweile befinden) original\n'
            '<<<ITEM>>>\n'
            '55 (a – Mittlerweile befinden) revision'
        )

        self.assertEqual(
            2,
            answer_markers.repair_sprachbausteine_inline_answers(parsed, markdown),
        )
        self.assertEqual(['a', 'a'], [item['questions'][0]['answer'] for item in parsed])

    def test_conflicting_editions_fail_closed(self):
        parsed = self._q55()
        markdown = (
            '55 (a - Mittlerweile befinden) original\n'
            '<<<ITEM>>>\n'
            '55 (b - Zurzeit haben) revision'
        )

        self.assertEqual(
            0,
            answer_markers.repair_sprachbausteine_inline_answers(parsed, markdown),
        )
        self.assertEqual('c', parsed[0]['questions'][0]['answer'])

    def test_duplicate_key_in_one_chunk_fails_closed(self):
        parsed = self._q55()
        markdown = (
            '55 (a - Mittlerweile befinden) first\n'
            '55 (a - Mittlerweile befinden) duplicate'
        )

        self.assertEqual(
            0,
            answer_markers.repair_sprachbausteine_inline_answers(parsed, markdown),
        )

    def test_missing_or_mismatched_option_fails_closed(self):
        parsed = self._q55()
        parsed[0]['questions'][0]['options'][0]['text'] = 'Different text'

        self.assertEqual(
            0,
            answer_markers.repair_sprachbausteine_inline_answers(
                parsed,
                '55 (a - Mittlerweile befinden)',
            ),
        )

    def test_duplicate_question_number_inside_item_fails_closed(self):
        parsed = self._q55()
        parsed[0]['questions'].append(dict(parsed[0]['questions'][0]))

        self.assertEqual(
            0,
            answer_markers.repair_sprachbausteine_inline_answers(
                parsed,
                '55 (a - Mittlerweile befinden)',
            ),
        )


class ParseEndpointPdfAnswerRepairTest(unittest.TestCase):
    def setUp(self):
        if main is None:
            self.skipTest('backend runtime dependencies are not installed')
        self.client = main.app.test_client()

    @patch('main.firestore_client.is_premium', return_value=True)
    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    @patch('main._call_gemini')
    def test_parse_repairs_then_strips_authoritative_marker(
        self,
        call_gemini,
        _authenticate,
        _rate_limit,
        _premium,
    ):
        response = _choice(
            options=[
                {'letter': 'a', 'text': 'Mittlerweile befinden [[PDF_CORRECT:mittlerweile befinden]]'},
                {'letter': 'b', 'text': 'Zurzeit haben'},
                {'letter': 'c', 'text': 'Zwischenzeitlich sind'},
            ],
        )
        call_gemini.return_value = json.dumps(response)

        result = self.client.post(
            '/api/parse',
            json={
                'section_type': 'sprachbausteine_teil2',
                'markdown': '[[PDF_CORRECT:mittlerweile befinden]]',
            },
            headers={'X-Exam-Trainer-Answer-Markers': 'v38'},
        )

        self.assertEqual(200, result.status_code)
        question = result.get_json()[0]['questions'][0]
        self.assertEqual('a', question['answer'])
        self.assertEqual('Mittlerweile befinden', question['options'][0]['text'])
        self.assertNotIn('PDF_CORRECT', json.dumps(result.get_json()))

    @patch('main.firestore_client.is_premium', return_value=True)
    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    @patch('main._call_gemini')
    def test_inline_key_is_scoped_to_sprachbausteine_teil2(
        self,
        call_gemini,
        _authenticate,
        _rate_limit,
        _premium,
    ):
        response = self.client.post
        parsed = _choice(answer='c', options=[
            {'letter': 'a', 'text': 'Mittlerweile befinden'},
            {'letter': 'b', 'text': 'Zurzeit haben'},
            {'letter': 'c', 'text': 'Zwischenzeitlich sind'},
        ])
        parsed[0]['questions'][0]['number'] = 55
        call_gemini.return_value = json.dumps(parsed)
        payload = {'markdown': '55 (a - Mittlerweile befinden)'}

        teil2 = response('/api/parse', json={
            **payload,
            'section_type': 'sprachbausteine_teil2',
        }, headers={'X-Exam-Trainer-Answer-Markers': 'v38'})
        beschwerde = response('/api/parse', json={
            **payload,
            'section_type': 'beschwerde',
        }, headers={'X-Exam-Trainer-Answer-Markers': 'v38'})

        self.assertEqual('a', teil2.get_json()[0]['questions'][0]['answer'])
        self.assertEqual('c', beschwerde.get_json()[0]['questions'][0]['answer'])

    @patch('main.firestore_client.is_premium', return_value=True)
    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    @patch('main._call_gemini')
    def test_legacy_parse_does_not_apply_v38_repair(
        self,
        call_gemini,
        _authenticate,
        _rate_limit,
        _premium,
    ):
        parsed = _choice(answer='c', options=[
            {'letter': 'a', 'text': 'Mittlerweile befinden'},
            {'letter': 'b', 'text': 'Zurzeit haben'},
            {'letter': 'c', 'text': 'Zwischenzeitlich sind'},
        ])
        parsed[0]['questions'][0]['number'] = 55
        call_gemini.return_value = json.dumps(parsed)

        result = self.client.post('/api/parse', json={
            'section_type': 'sprachbausteine_teil2',
            'markdown': '55 (a - Mittlerweile befinden)',
        })

        self.assertEqual('c', result.get_json()[0]['questions'][0]['answer'])

    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    @patch('main._inject_answer_markers')
    @patch('main._inject_legacy_answer_markers', return_value='legacy marker text')
    @patch('main.pdfminer.high_level.extract_text', return_value='legacy text\n')
    def test_convert_is_byte_compatible_without_v38_opt_in(
        self,
        _extract,
        legacy_inject,
        inject,
        _authenticate,
        _rate_limit,
    ):
        result = self.client.post(
            '/api/convert',
            data=b'%PDF fixture',
            content_type='application/octet-stream',
        )

        self.assertEqual(200, result.status_code)
        self.assertEqual('legacy marker text', result.get_json()['markdown'])
        legacy_inject.assert_called_once()
        inject.assert_not_called()

    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    @patch('main._inject_answer_markers', return_value='v38 marker text')
    @patch('main._inject_legacy_answer_markers')
    @patch('main.pdfminer.high_level.extract_text', return_value='legacy text\n')
    def test_convert_enables_markers_only_for_exact_v38_header(
        self,
        _extract,
        legacy_inject,
        inject,
        _authenticate,
        _rate_limit,
    ):
        result = self.client.post(
            '/api/convert',
            data=b'%PDF fixture',
            content_type='application/octet-stream',
            headers={'X-Exam-Trainer-Answer-Markers': 'v38'},
        )

        self.assertEqual(200, result.status_code)
        self.assertEqual('v38 marker text', result.get_json()['markdown'])
        inject.assert_called_once()
        legacy_inject.assert_not_called()

    @patch('main._rate_limit_ok', return_value=True)
    @patch('main._authenticate', return_value='uid-1')
    def test_parse_rejects_non_object_json_bodies(self, _authenticate, _rate_limit):
        # Same bug class as /api/cache and /api/device: a valid-JSON body
        # that isn't an object used to reach body.get(...), raising
        # AttributeError and returning a raw 500.
        for bad_body in ([], 'a string', 42, None, True):
            response = self.client.post(
                '/api/parse',
                data=json.dumps(bad_body),
                content_type='application/json',
            )
            self.assertEqual(
                response.status_code, 400,
                msg=f'non-object body {bad_body!r} did not get a clean 400')
            self.assertNotIn(b'Traceback', response.data)


if __name__ == '__main__':
    unittest.main()

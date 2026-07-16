import sys
import types
import unittest
from unittest.mock import patch

try:
    import edge_tts  # noqa: F401
except ModuleNotFoundError:
    sys.modules['edge_tts'] = types.SimpleNamespace(Communicate=None)

import tts

try:
    import main
except ModuleNotFoundError as e:
    if e.name != 'flask':
        raise
    main = None


class TtsVoiceSelectionTest(unittest.TestCase):
    def test_arbitrary_names_are_not_hardcoded_to_a_gender(self):
        self.assertEqual(tts._gender('Andrea Faber'), 'unknown')
        self.assertEqual(tts._gender('Ein Neuer Name'), 'unknown')

    def test_missing_voice_gender_keeps_legacy_heuristics(self):
        voice = tts.voice_for('Herr Becker', 'Guten Tag')

        self.assertIn(voice, tts.MALE_VOICES)
        self.assertNotIn(voice, tts.FEMALE_VOICES)

    def test_unknown_voice_gender_keeps_legacy_heuristics(self):
        voice = tts.voice_for('Frau Becker', 'Guten Tag', 'unknown')

        self.assertIn(voice, tts.FEMALE_VOICES)
        self.assertNotIn(voice, tts.MALE_VOICES)

    def test_explicit_voice_gender_overrides_speaker_heuristics(self):
        female_voice = tts.voice_for('Herr Becker', 'Guten Tag', 'female')
        male_voice = tts.voice_for('Andrea Faber', 'Guten Tag', 'male')

        self.assertIn(female_voice, tts.FEMALE_VOICES)
        self.assertNotIn(female_voice, tts.MALE_VOICES)
        self.assertIn(male_voice, tts.MALE_VOICES)
        self.assertNotIn(male_voice, tts.FEMALE_VOICES)

    def test_explicit_voice_gender_without_speaker_is_stable_across_chunks(self):
        first_chunk = tts.voice_for('', 'Erster Teil der Nachricht', 'female')
        second_chunk = tts.voice_for('', 'Zweiter Teil der Nachricht', 'female')

        self.assertEqual(first_chunk, second_chunk)
        self.assertIn(first_chunk, tts.FEMALE_VOICES)


class TtsEndpointContractTest(unittest.TestCase):
    def setUp(self):
        if main is None:
            self.skipTest('Flask is not installed in this Python environment')
        self.client = main.app.test_client()

    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_old_client_without_voice_gender_still_accepts_speaker(
            self, _authenticate, _rate_limit_ok):
        seen = {}

        async def fake_synthesize(text, voice):
            seen['voice'] = voice
            return b'audio'

        with patch.object(main.tts, 'synthesize', side_effect=fake_synthesize):
            response = self.client.post(
                '/api/tts',
                json={'text': 'Hallo', 'speaker': 'Andrea Faber'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'audio')
        self.assertIn(seen['voice'], tts.MALE_VOICES + tts.FEMALE_VOICES)

    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_endpoint_voice_gender_override_beats_name_and_role(
            self, _authenticate, _rate_limit_ok):
        seen = {}

        async def fake_synthesize(text, voice):
            seen['voice'] = voice
            return b'audio'

        with patch.object(main.tts, 'synthesize', side_effect=fake_synthesize):
            response = self.client.post(
                '/api/tts',
                json={
                    'text': 'Guten Tag',
                    'speaker': 'Herr Becker',
                    'voice_gender': 'female',
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(seen['voice'], tts.FEMALE_VOICES)
        self.assertNotIn(seen['voice'], tts.MALE_VOICES)

    @patch.object(main, '_rate_limit_ok', return_value=True)
    @patch.object(main, '_authenticate', return_value='uid-1')
    def test_invalid_voice_gender_returns_safe_validation_error(
            self, _authenticate, _rate_limit_ok):
        with patch.object(main.tts, 'synthesize') as synthesize:
            responses = [
                self.client.post(
                    '/api/tts',
                    json={
                        'text': 'Guten Tag',
                        'speaker': 'Andrea Faber',
                        'voice_gender': 'feminine',
                    },
                ),
                self.client.post(
                    '/api/tts',
                    json={
                        'text': 'Guten Tag',
                        'speaker': 'Andrea Faber',
                        'voice_gender': None,
                    },
                ),
            ]

        for response in responses:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(
                response.get_json(),
                {'error': 'voice_gender must be one of: female, male, unknown'},
            )
        synthesize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
